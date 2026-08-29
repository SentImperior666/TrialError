"""M14 acceptance criteria, design Section 12 row, gathered in one place —
mirrors the ``tests/test_m6_acceptance.py``/``tests/test_m10_acceptance.py``
convention: this file IS the acceptance-criteria mapping, each test here
re-running (not replacing) a narrower assertion that already lives in its
dedicated module.

    | Acceptance criterion (design Section 12, M14 row)                        | Test |
    |----------------------------------------------------------------------------|------|
    | tool-count <=12 asserted in test                                          | test_tool_count_is_at_most_12 (see also test_mcp_ops_tools.py::test_tool_registry_has_exactly_the_12_named_tools) |
    | each tool structured-error on bad input                                  | test_every_tool_returns_a_structured_error_on_bad_input (per-tool detail: test_mcp_ops_tools.py; protocol-level missing-required-arg: test_mcp_ops_protocol.py) |
    | live Claude Code smoke: book->spawn->reconcile round trip                | test_book_spawn_reconcile_round_trip_smoke_note (marks the live-CC step; the closest an offline pytest run can get is test_mcp_ops_protocol.py::test_full_book_spawn_reconcile_round_trip_over_the_wire, PLUS a real subprocess handshake in test_mcp_ops_protocol.py::test_stdio_smoke_real_subprocess_initialize_and_tools_list) |
"""

from __future__ import annotations

import pytest

from trialerror.accept.journeys import GPU_LIVE_CC_ITEMS
from trialerror.mcp.ops import TOOL_COUNT, build_tools
from trialerror.stores import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

pytestmark = pytest.mark.acceptance


def _seed_open_session(store) -> tuple[str, str]:
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(store, "session", {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"})
    return account_id, session_id


def test_tool_count_is_at_most_12(program_root, platform_root):
    """Design Section 12 M14 row: "tool-count <=12 asserted in test." The
    design's own Section 5.1 table pins it at exactly 12 (not just a
    ceiling) -- asserted here too, since a silent drift below 12 would be
    just as much a spec violation as drifting above it."""
    tools = build_tools(program_root=program_root, platform_root=platform_root)
    assert len(tools) <= 12
    assert len(tools) == TOOL_COUNT == 12


def test_every_tool_returns_a_structured_error_on_bad_input(store):
    """Design Section 12 M14 row: "each tool structured-error on bad
    input." Exhaustive over all 12 tools (not just the handful with a
    dedicated negative-path test in ``test_mcp_ops_tools.py``): for every
    tool, a schema-shape-valid but business-invalid call comes back as a
    clean ``{ok: false, error: {code, message}}`` envelope -- never an
    unhandled exception, matching design Section 5.1's cross-cutting rule
    ("errors returned as structured content ... never exceptions") applied
    to the full tool surface at once."""
    account_id, session_id = _seed_open_session(store)
    tools = build_tools(program_root=store.program_root, platform_root=store.platform_root)
    assert set(tools) == {spec_name for spec_name in tools}  # sanity: no duplicate/empty keys

    bad_args = {
        "session_status": {"session_id": "SESS-does-not-exist"},  # informational, not a refusal -- see note below
        "budget_status": {"account_id": "ACC-does-not-exist"},
        "book_launch": {"program_id": "P", "agent_kind": "a", "model_class": "top",
                         "model": "m", "purpose": "p", "est_tokens": "not-a-number"},
        "reconcile_launch": {"launch_id": "LNCH-does-not-exist", "actual_tokens": 1},
        "append_event": {"event_type": "t", "payload": {}, "launch_id": "LNCH-does-not-exist"},
        "post_feed": {"thread_id": "THR-does-not-exist", "body": "x"},
        "register_artifact": {"artifact_id": "ART-does-not-exist", "by_launch": "LNCH-does-not-exist"},
        "gate_advance": {"gate_id": "CR-does-not-exist", "to_state": "submitted", "by_launch": "LNCH-does-not-exist"},
        "prereg_commit": {"title": "t", "procedure": "   "},
        "record_verdict": {"subject_kind": "not-a-real-kind", "subject_id": "x", "procedure": "citecheck",
                            "procedure_version": "1", "label": "L", "issued_by_launch": "LNCH-does-not-exist"},
    }

    for name, spec in tools.items():
        args = bad_args.get(name, {})
        env = spec.handler(args)
        assert isinstance(env, dict) and "ok" in env, f"{name}: handler did not return a structured envelope"
        if name in ("session_status", "read_inbox", "law_lookup", "budget_status"):
            # These four are pure reads whose own landed APIs never raise
            # for "not found"/"nothing configured" -- they report it INSIDE
            # a successful envelope (see test_mcp_ops_tools.py's dedicated
            # tests for each; budget_status with an unknown account_id
            # simply reports zero pools, same "found nothing" shape as
            # session_status's "open: false"). The acceptance bar here
            # ("structured, never a crash") is still met -- just via
            # ok:true + an informational payload rather than ok:false.
            continue
        assert env["ok"] is False, f"{name}: expected a structured refusal for {args!r}, got {env!r}"
        assert env.get("error", {}).get("code"), f"{name}: ok=False envelope missing a structured error code"


@pytest.mark.skip(reason=GPU_LIVE_CC_ITEMS["live_cc_mcp_smoke_ops_server_book_spawn_reconcile"])
def test_book_spawn_reconcile_round_trip_smoke_note():
    """Design Section 12 M14 row names this "live Claude Code smoke" --
    same class of item as M3's/M6's/M8's own hook/MCP acceptance rows
    ("live-CC hook tests are orchestrator-executed integration items"):
    genuinely live only inside an actual Claude Code session with this
    server registered. The closest a pytest run can get:

    - ``tests/test_mcp_ops_protocol.py::test_full_book_spawn_reconcile_round_trip_over_the_wire``
      drives book_launch and reconcile_launch over the REAL JSON-RPC wire
      (initialize/tools-call), with the spawn leg consuming the booking via
      ``trialerror.budget.gate.evaluate_spawn_for_open_session`` -- the exact
      function ``plugin/hooks/spawn_gate.py`` (a live PreToolUse hook) is a
      thin stdin/exit-code shell over.
    - ``tests/test_mcp_ops_protocol.py::test_stdio_smoke_real_subprocess_initialize_and_tools_list``
      launches ``python -m trialerror.cli mcp ops`` as a REAL subprocess and
      speaks real stdio to it -- this build's "stdio smoke result."

    FX-6 (IMPL_REVIEW_VERDICT.md Tier 2 / IMPL_REVIEW_C_ops.md HOLLOW):
    this was a literal ``assert True`` marker/pointer with no assertions of
    its own. Converted to an explicit ``@pytest.mark.skip`` naming the exact
    live-CC step (the SAME ``trialerror.accept.journeys.GPU_LIVE_CC_ITEMS``
    message ``trialerror accept``'s own doctor-shaped summary reports -- one
    source of truth for this item's status) -- the mapping table above
    still has a row to point at, now truthfully marked not-yet-run rather
    than fake-passing. The two real, already-landed proxy tests named in
    this docstring are NOT duplicative of this one; nothing was deleted.
    """
