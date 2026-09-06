"""``static/console_render.js`` -- the Console's renderers, tested as code.

The dashboard's renderers used to be unreachable by any test: 2,600 lines of
inline script inside ``dashboard.html``, asserted on only as pre-JS HTML
strings. Sweep section 3.11's answer is a DOM-level harness with zero npm
dependencies -- ``tests/_dom_shim.js`` (a ~280-line tree that implements the
handful of methods the page's own element helper calls) and
``tests/_console_render_harness.js`` (loads the SHIPPED render file into a vm
context with the shim standing in for the browser, calls one function, prints
the resulting tree as JSON). This module drives that harness.

Node is not a project dependency, so every test here skips with a stated
reason when it is absent -- the repo's enumerated-skip discipline: a skip that
does not say why is indistinguishable from a test that never ran.

Coverage: C4's half of the contract (the shim and harness themselves, the two
shared primitives, the card registry) plus K4's -- sweep tests 1-23, one test
function per numbered item, named in the sweep's own words.

Two of them go through ``node -e`` and ``require()`` rather than the harness:
``computeHealth``'s matrix and ``compressIdleGaps``'s monotonicity sweep are
pure over JSON and want eight or thirty-six thousand cases, and one subprocess
per case is a slow way to learn nothing extra.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "tests" / "_console_render_harness.js"
CONSOLE_RENDER_JS = REPO_ROOT / "trialerror" / "dashboard" / "static" / "console_render.js"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(
    NODE is None, reason="node not on PATH -- Console DOM tests need Node >= 18"
)

#: A fixed clock, so every rendered age and every bar edge is a constant. The
#: renderers take `nowMs` rather than reading the wall clock, which is the
#: only reason a timeline is assertable at all.
NOW_ISO = "2026-09-05T02:00:00.000Z"
NOW_MS = 1788573600000  # Date.parse(NOW_ISO)


def run_harness(fn: str, args: list | None = None, *, select: str | None = None, expect_ok: bool = True) -> dict:
    """Call one renderer function under Node and return the harness's JSON.

    Arguments go through a file rather than the command line: fixtures are
    nested JSON, and Windows' command-line quoting mangles them.
    """
    cmd = [NODE, str(HARNESS), "--fn", fn]
    with tempfile.TemporaryDirectory() as tmp:
        if args is not None:
            args_file = Path(tmp) / "args.json"
            args_file.write_text(json.dumps(args), encoding="utf-8")
            cmd += ["--args-file", str(args_file)]
        if select:
            cmd += ["--select", select]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=str(REPO_ROOT))
    assert proc.stdout, f"harness printed nothing; stderr={proc.stderr!r}"
    payload = json.loads(proc.stdout)
    if expect_ok:
        assert payload.get("kind") != "error", payload.get("error")
        assert proc.returncode == 0, proc.stderr
    return payload


def node_json(expression: str):
    """Evaluate one expression against the SHIPPED module and read back JSON.

    ``TE`` is the module. Used only for pure functions whose test wants many
    cases in one process."""
    script = (
        "const TE = require(%s);\n"
        "process.stdout.write(JSON.stringify((() => { %s })()));"
        % (json.dumps(CONSOLE_RENDER_JS.as_posix()), expression)
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# tree helpers
# ---------------------------------------------------------------------------


def find_all(tree: dict, predicate) -> list[dict]:
    out = []
    if predicate(tree):
        out.append(tree)
    for child in tree.get("children", []):
        out.extend(find_all(child, predicate))
    return out


def by_class(tree: dict, cls: str) -> list[dict]:
    return find_all(tree, lambda n: cls in n.get("classes", []))


def by_tag(tree: dict, tag: str) -> list[dict]:
    return find_all(tree, lambda n: n.get("tag") == tag)


def one(nodes: list[dict], what: str) -> dict:
    assert len(nodes) == 1, f"expected exactly one {what}, got {len(nodes)}"
    return nodes[0]


def elements(tree: dict) -> list[dict]:
    return [c for c in tree.get("children", []) if c.get("tag") != "#text"]


def reading(tree: dict, label: str) -> dict:
    """The ``.reading-row`` whose key is ``label``."""
    rows = [
        r for r in by_class(tree, "reading-row")
        if any(c.get("text") == label for c in by_class(r, "k"))
    ]
    return one(rows, f"reading row {label!r}")


# ---------------------------------------------------------------------------
# fixtures
#
# Built here rather than captured. Sweep section 3.11 names three live
# captures; a shape the test states outright is easier to read than a 40 KB
# JSON file that three assertions reach into, and it cannot go stale against
# a builder change without the test saying so.
# ---------------------------------------------------------------------------


def session_panel(**over) -> dict:
    readiness = {
        "ready": False,
        "problems": ["5 launch(es) still PROVISIONAL/RUNNING under this session"],
        "pin_check": None,
        "dangling_launches": [
            {
                "launch_id": f"LNCH-000000000000000000000{i}", "agent_kind": "implementer",
                "purpose": f"lane c step {i}", "state": "PROVISIONAL",
                "booked_ts": "2026-09-05T01:30:00.000Z", "est_tokens": 1_800_000,
            }
            for i in range(5)
        ],
    }
    panel = {
        "status": "ok",
        "open_session": {
            "session_id": "SESS-01M1R8J31R24P6781WT1G05PZC",
            "account_id": "ACC-01M1R8J31R24P6781WT1G05PZC",
            "opened_ts": "2026-09-05T00:00:00.000Z",
            "status": "open",
            "boot_bundle_stats": {"boot_pin_version": None, "boot_bundle_sha": "a" * 64, "queue": []},
            "close_readiness": readiness,
            "unread_inbox_count": 0,
            "hook_alive_count": 3,
            "active_jobs_count": 1,
            "timeline": None,
        },
        "recent_sessions": [
            {"session_id": "SESS-OLD", "account_id": "ACC-1", "status": "closed",
             "opened_ts": "2026-09-04T08:00:00.000Z", "closed_ts": "2026-09-04T20:00:00.000Z"},
        ],
    }
    panel.update(over)
    return panel


def budget_panel(**over) -> dict:
    panel = {
        "status": "ok",
        "plan_quota": {"available": True, "fresh": True, "age_s": 30,
                       "captured_ts": "2026-09-05T01:59:30.000Z", "model": "opus",
                       "windows": {"five_hour": {"used_percentage": 96,
                                                 "resets_at": "2026-09-05T04:00:00.000Z"}}},
        "accounts": [{
            "account": {"account_id": "ACC-1", "label": "primary", "created_ts": "2026-01-01T00:00:00.000Z"},
            "budget_status": {"pools": [{
                "pool_id": "POOL-1", "model_class": "top", "period": "weekly",
                "cap_tokens": 1000, "soft_cap": 950, "projected_billed_tokens": 400,
                "spent_visible_tokens": 200, "committed_visible_tokens": 100,
                "billed_multiplier": 2.75, "headroom_tokens": 600,
                "over_soft": False, "over_hard": False,
            }], "defer_advisories": []},
            "launch_state_counts": {"PROVISIONAL": 1},
        }],
        "launch_state_counts_total": {"PROVISIONAL": 1, "RECONCILED": 4},
        "dangling_bookings": [],
    }
    panel.update(over)
    return panel


def job(**over) -> dict:
    row = {
        "job_id": "JOB-01M1R8J31R24P6781WT1G05PZC", "kind": "custom", "subject": "arxiv_index_build",
        "payload": {"handler": "arxiv_index_build"}, "state": "complete",
        "claimed_by": None, "lease_expires_ts": None, "heartbeat_ts": None,
        "attempts": 1, "max_attempts": 3, "next_attempt_ts": None,
        "failure_class": None, "last_error": None,
        "checkpoint": {"rows_ingested": 3569548},
        "created_ts": "2026-09-04T22:00:00.000Z", "settled_ts": "2026-09-05T02:00:00.000Z",
        "duration_s": 14400,
    }
    row.update(over)
    return row


def jobs_panel(rows, **over) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    panel = {
        "status": "ok", "state_counts": counts, "live_jobs": [], "stale_leases": [],
        "recent_jobs": rows,
        "offload": {"available": False, "counts": {"pending": 0, "claimed": 0, "done": 0, "failed": 0},
                    "awaiting": 0, "jobs": {}},
    }
    panel.update(over)
    return panel


def check(name, status, category="stores", message="", details=None) -> dict:
    return {"name": name, "status": status, "category": category,
            "message": message, "details": details or {}}


def doctor_panel(checks) -> dict:
    summary = {"total": len(checks), "passed": 0, "failed": 0, "warned": 0, "skipped": 0}
    for c in checks:
        summary[{"pass": "passed", "fail": "failed", "warn": "warned", "skip": "skipped"}[c["status"]]] += 1
    return {"status": "ok",
            "last_run": {"schema": 1, "ran_ts": "2026-09-05T01:29:47.000Z",
                         "summary": summary, "checks": checks}}


def timeline_fixture(spans=None, instants=None, end_ts=None) -> dict:
    return {
        "window": {"start_ts": "2026-09-05T00:00:00.000Z", "end_ts": end_ts},
        "spans": spans if spans is not None else [],
        "instants": instants or [],
        "truncated": {"spans_dropped": 0, "instants_dropped": 0, "events_scan_limited": False},
    }


# ---------------------------------------------------------------------------
# the file itself -- assertions that need no Node at all
# ---------------------------------------------------------------------------


def test_console_render_js_never_reaches_for_the_global_node_factory():
    """The one rule that makes this file testable: nodes come from the
    injected helper. A single global ``createElement`` here would work in the
    browser and fail every test in this module for a reason that reads as a
    harness bug."""
    source = CONSOLE_RENDER_JS.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(("*", "/*", "//"))
    )
    assert "document." not in body, "console_render.js must build nodes through the injected `h`"


def test_console_render_js_is_inlinable():
    """``</script`` anywhere in the file would end the element early when
    export.py inlines it into the static snapshot."""
    assert "</script" not in CONSOLE_RENDER_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# the shim and harness themselves
# ---------------------------------------------------------------------------


@requires_node
def test_harness_loads_the_shipped_file_and_reports_unknown_functions():
    payload = run_harness("noSuchFunction", [], expect_ok=False)
    assert payload["kind"] == "error"
    assert "noSuchFunction" in payload["error"]


@requires_node
def test_harness_reports_a_renderer_that_reaches_for_innerhtml():
    """The shim refuses ``h(tag, {html})`` on purpose: a renderer that builds
    markup strings can inject a fenced quote into the page as HTML. Proving
    the refusal here means a later renderer cannot acquire the habit quietly."""
    payload = run_harness("h2", ["x", {"attrs": {"html": "<b>no</b>"}}], expect_ok=False)
    assert payload["kind"] == "error"
    assert "html" in payload["error"]


# ---------------------------------------------------------------------------
# shared primitives (spec section 3 item (i): introduced here, reused by
# Evidence, Feed and Determinations)
# ---------------------------------------------------------------------------


@requires_node
def test_h2_is_a_real_heading_carrying_the_title_class():
    tree = run_harness("h2", ["SESSION"])["tree"]
    assert tree["tag"] == "h2"
    assert tree["classes"] == ["title"]
    assert tree["text"] == "SESSION"


@requires_node
def test_h2_keeps_extra_classes_and_attributes():
    tree = run_harness("h2", ["POOLS", {"className": "card-title", "attrs": {"id": "x"}}])["tree"]
    assert tree["classes"] == ["title", "card-title"]
    assert tree["attrs"]["id"] == "x"


@requires_node
def test_row_button_is_a_button_with_one_cell_per_column():
    tree = run_harness("rowButton", [["contradicts", "C-0004", "same anchor"], {}])["tree"]
    assert tree["tag"] == "button"
    assert tree["attrs"]["type"] == "button"
    assert "row-button" in tree["classes"]
    cells = by_class(tree, "row-button__cell")
    assert [c["text"] for c in cells] == ["contradicts", "C-0004", "same anchor"]


@requires_node
def test_row_button_wires_a_click_handler_when_enabled():
    tree = run_harness("rowButton", ["OPEN", {"onClick": {"__fn": "open"}}])["tree"]
    assert tree["listeners"] == ["click"]
    assert "disabled" not in tree["attrs"]


@requires_node
def test_row_button_survives_a_missing_handler():
    """A row with nothing to do yet is still a row, not a crash."""
    tree = run_harness("rowButton", ["OPEN", {}])["tree"]
    assert tree["listeners"] == []
    assert "disabled" not in tree["attrs"]


@requires_node
def test_disabled_row_button_states_its_reason_and_takes_no_handler():
    """DASHBOARD_V2_API section 12.11: a control drawn disabled must say why.
    The reason IS the disabled flag, so it cannot be omitted by accident."""
    tree = run_harness("rowButton", ["SEND TO DETERMINATIONS", {"disabled": "no callable exists yet"}])["tree"]
    assert tree["attrs"]["disabled"] == "disabled"
    assert tree["attrs"]["aria-disabled"] == "true"
    assert tree["attrs"]["title"] == "no callable exists yet"
    assert tree["listeners"] == []


@requires_node
def test_selected_row_button_says_so_to_a_screen_reader_too():
    tree = run_harness("rowButton", ["EV-1", {"selected": True}])["tree"]
    assert "is-selected" in tree["classes"]
    assert tree["attrs"]["aria-current"] == "true"


# ---------------------------------------------------------------------------
# the card registry
# ---------------------------------------------------------------------------


@requires_node
def test_render_into_paints_every_registered_card_and_reports_which():
    """K4 registers all eight. What ``renderInto`` returns is what tells
    dashboard.html which cards it does NOT have to fall back on the generic
    renderer for -- and a card that quietly stopped registering would show up
    here as a name missing from the list, not as an empty box in a browser."""
    payload = run_harness("renderInto", [
        {"__targets": ["session", "pools", "ledger", "timeline", "jobs", "diagnostics", "gates", "corpus"]},
        {"panels": {}},
        {"nowMs": NOW_MS},
    ])
    assert payload["value"] == [
        "session", "pools", "ledger", "timeline", "jobs", "diagnostics", "gates", "corpus"
    ]
    for name, tree in payload["targets"].items():
        assert tree["children"], f"{name} was registered but painted nothing"


@requires_node
def test_render_into_skips_a_card_with_no_container():
    payload = run_harness("renderInto", [{"__targets": ["jobs"]}, {"panels": {}}, {"nowMs": NOW_MS}])
    assert payload["value"] == ["jobs"]


@requires_node
def test_card_order_is_the_order_the_canvas_lays_the_grid_out():
    """The registry refuses a name that is not in CARD_ORDER, so the list is
    load-bearing: a typo in a later build's ``registerCard`` fails loudly
    instead of registering a card nothing ever paints."""
    assert node_json("return TE.CARD_ORDER;") == [
        "session", "pools", "ledger", "timeline", "jobs", "diagnostics", "gates", "corpus"
    ]


@requires_node
def test_register_card_refuses_a_name_outside_card_order():
    payload = run_harness("registerCard", ["poolz", {"__fn": "render"}], expect_ok=False)
    assert payload["kind"] == "error"
    assert "poolz" in payload["error"]


# ===========================================================================
# sweep test 1
# ===========================================================================


@requires_node
def test_no_generic_tables_in_console_cards():
    """M-CON-5a: every card used to open with a ``status ok`` row and render
    its payload as nested key/value tables. The bespoke cards own their
    layout now; the only table left on the Console is the JOBS one, which is
    a real table of real columns."""
    cases = {
        "renderSessionCard": session_panel(),
        "renderPoolsCard": budget_panel(),
        "renderLedgerCard": budget_panel(),
        "renderGatesCard": {"status": "ok", "pending_edits": [], "gate_state_counts": {"draft": 1},
                            "gate_verdict_counts": {}, "reproduction_status_counts": {}},
        "renderCorpusCard": {"status": "ok",
                             "counts": {"sources": 1, "documents": 2, "chunks": 3, "quote_anchors": 4},
                             "stale_anchors": 0, "extract_coverage": {"pending_records": 0},
                             "summary_coverage": {"documents_with_current_summary": 1, "total_documents": 2},
                             "license_tier_counts": {}},
        "renderDiagnosticsCard": doctor_panel([check("xid_dangling", "pass")]),
    }
    for fn, panel in cases.items():
        tree = run_harness(fn, [panel, {"nowMs": NOW_MS}])["tree"]
        assert not by_class(tree, "kv-table"), f"{fn} rendered a generic kv table"
        assert not by_class(tree, "rows-table"), f"{fn} rendered a generic rows table"
        keys = [n.get("text") for n in by_class(tree, "k")]
        assert "status" not in keys, f"{fn} still opens with a `status` row"


# ===========================================================================
# sweep tests 2-4 -- SESSION
# ===========================================================================


@requires_node
def test_session_readiness_callout_is_first_and_verbatim():
    """console-5: the answer to "why can't I close?" is a plain-English
    string the backend already computed, and it used to render last, three
    levels deep inside a nested table."""
    panel = session_panel()
    tree = run_harness("renderSessionCard", [panel, {"nowMs": NOW_MS}])["tree"]
    first = elements(tree)[0]
    assert "warn-callout" in first["classes"]
    assert one(by_class(first, "head"), "callout head")["text"] == "▲ 5 IN FLIGHT"
    problem = panel["open_session"]["close_readiness"]["problems"][0]
    assert problem in first["text"], "the backend's own sentence must survive verbatim"
    assert len(by_class(tree, "dangling-row")) == 5

    boot_pin = reading(tree, "BOOT PIN")
    assert "NO PIN" in boot_pin["text"]
    assert "status--warn" in boot_pin["classes"], "a session with no law pin is a gap, not a blank"


@requires_node
def test_session_invariant_violation_uses_crit_band():
    tree = run_harness("renderSessionCard", [
        {"status": "invariant_violation", "message": "2 sessions are open"}, {"nowMs": NOW_MS},
    ])["tree"]
    band = one(by_class(tree, "callout--crit"), "crit band")
    assert "2 sessions are open" in band["text"]
    assert not by_class(tree, "warn-callout")


@requires_node
def test_session_none_open_is_a_reading():
    panel = session_panel(open_session=None)
    tree = run_harness("renderSessionCard", [panel, {"nowMs": NOW_MS}])["tree"]
    assert "○ NONE OPEN" in one(by_class(tree, "callout--neutral"), "neutral callout")["text"]
    assert "trialerror session open" in tree["text"]
    assert by_class(tree, "history-row"), "history is still shown when nothing is open"


# ===========================================================================
# sweep tests 5-6 -- POOLS
# ===========================================================================


@requires_node
def test_pools_quota_freshness_first():
    """console-10: ``plan_quota.fresh`` is the trust flag for every other
    number on the card, and it used to render last."""
    no_quota = budget_panel(plan_quota={"available": False, "fresh": False, "windows": {},
                                        "note": "no statusline quota captured"})
    tree = run_harness("renderPoolsCard", [no_quota, {"nowMs": NOW_MS}])["tree"]
    first = elements(tree)[0]
    assert "callout--neutral" in first["classes"], "an uncaptured quota is neutral, not a warning"
    assert "no statusline quota captured" in first["text"]

    stale = budget_panel(plan_quota={"available": True, "fresh": False, "age_s": 7200,
                                     "captured_ts": "2026-09-05T00:00:00.000Z", "windows": {}})
    first = elements(run_harness("renderPoolsCard", [stale, {"nowMs": NOW_MS}])["tree"])[0]
    assert "warn-callout" in first["classes"]
    assert "2h" in first["text"]

    tree = run_harness("renderPoolsCard", [budget_panel(), {"nowMs": NOW_MS}])["tree"]
    assert by_class(tree, "meter__fill")[0]["attrs"]["style"] == "width: 96%"
    assert by_class(tree, "meter__tick")[0]["attrs"]["style"] == "left: 95%"
    assert any(c["text"] == "AT TRIGGER" for c in by_class(tree, "chip"))


@requires_node
def test_pool_meter_tick_is_soft_cap():
    """Every meter on the page shares one 0-100 ramp with a tick at ITS OWN
    trigger -- for a pool that is the soft cap, not the 95% the plan windows
    use."""
    quiet = {"available": False, "windows": {}, "note": "n/a"}
    tree = run_harness("renderPoolsCard", [budget_panel(plan_quota=quiet), {"nowMs": NOW_MS}])["tree"]
    fill = one(by_class(tree, "meter__fill"), "pool meter fill")
    assert fill["attrs"]["style"] == "width: 40%"
    assert one(by_class(tree, "meter__tick"), "pool tick")["attrs"]["style"] == "left: 95%"
    assert "meter__fill--warn" not in fill["classes"]

    over = budget_panel(plan_quota=quiet)
    over["accounts"][0]["budget_status"]["pools"][0]["over_hard"] = True
    tree = run_harness("renderPoolsCard", [over, {"nowMs": NOW_MS}])["tree"]
    assert any(c["text"] == "OVER HARD" for c in by_class(tree, "chip"))


# ===========================================================================
# sweep test 7 -- LAUNCH LEDGER
# ===========================================================================


@requires_node
def test_ledger_prints_zero_dangling_as_reading():
    """The canvas's own footnote: "a dangling booking is the one thing that
    blocks a close; zero is a reading"."""
    tree = run_harness("renderLedgerCard", [budget_panel(), {"nowMs": NOW_MS}])["tree"]
    labels = [one(by_class(r, "k"), "key")["text"] for r in by_class(tree, "reading-row")]
    assert labels[:4] == ["RUNNING", "BOOKED, NOT YET SPAWNED", "RECONCILED", "DANGLING"]
    row = reading(tree, "DANGLING")
    assert row["text"].endswith("DANGLING 0")
    assert "status--settled" in row["classes"]

    with_dangling = budget_panel(dangling_bookings=[{
        "launch_id": "LNCH-0000000000000000000001", "agent_kind": "implementer",
        "purpose": "a booking whose session crashed", "state": "PROVISIONAL",
        "booked_ts": "2026-09-05T00:30:00.000Z", "booking_ttl_s": 3600,
    }])
    tree = run_harness("renderLedgerCard", [with_dangling, {"nowMs": NOW_MS}])["tree"]
    assert "status--crit" in reading(tree, "DANGLING")["classes"]
    assert len(by_class(tree, "dangling-row")) == 1


# ===========================================================================
# sweep tests 8-13 -- JOBS
# ===========================================================================


@requires_node
def test_jobs_table_columns_and_no_raw_json():
    """console-3, the operator-named wall: 15 union columns one character
    wide, with the ingest checkpoint printed into a cell as raw JSON."""
    tree = run_harness("renderJobsCard", [jobs_panel([job()]), {"nowMs": NOW_MS, "snapshot": None}])["tree"]
    headers = [th["text"] for th in by_tag(tree, "th")]
    assert headers == ["ID", "KIND AND SUBJECT", "STATE", "HEARTBEAT", "LEASE / DURATION", "PROGRESS", "Δ"]
    progress = one(by_class(tree, "cell-progress"), "progress cell")
    assert "3.57M rows" in progress["text"]
    assert "{" not in progress["text"]
    assert one(by_class(tree, "cell-subject"), "subject cell")["text"] == "custom · arxiv_index_build"
    assert one(by_class(tree, "cell-state"), "state cell")["text"] == "✓ DONE"
    assert one(by_class(tree, "cell-lease"), "lease cell")["text"] == "4h 0m"


@requires_node
def test_jobs_two_layer_colorer():
    """The k9s pattern: layer 1 is the ledger state, layer 2 overrides it
    from domain facts. Without layer 2 a claimed job whose lease expired
    twenty minutes ago reads exactly like a healthy one."""
    rows = [
        job(job_id="JOB-EXPIRED", state="running", lease_expires_ts="2026-09-05T01:00:00.000Z",
            heartbeat_ts="2026-09-05T01:59:00.000Z", checkpoint=None, settled_ts=None, duration_s=None),
        job(job_id="JOB-LATE", state="running", lease_expires_ts="2026-09-05T03:00:00.000Z",
            heartbeat_ts="2026-09-05T01:45:00.000Z", checkpoint=None, settled_ts=None, duration_s=None),
        job(job_id="JOB-RETRY", state="pending", next_attempt_ts="2026-09-05T02:25:00.000Z",
            failure_class="environmental", attempts=1, checkpoint=None, settled_ts=None, duration_s=None),
        job(job_id="JOB-GONE", state="abandoned", checkpoint=None, settled_ts=None, duration_s=None),
    ]
    tree = run_harness("renderJobsCard", [jobs_panel(rows), {"nowMs": NOW_MS, "snapshot": None}])["tree"]
    cells = {}
    for r in by_class(tree, "job-row"):
        full = one(by_class(r, "cell-id"), "id cell")["children"][0]["attrs"]["title"]
        cells[full] = one(by_class(r, "cell-state"), "state cell")

    assert "LEASE EXPIRED" in cells["JOB-EXPIRED"]["text"]
    assert by_class(cells["JOB-EXPIRED"], "status--crit")
    assert "HEARTBEAT LATE" in cells["JOB-LATE"]["text"]
    assert by_class(cells["JOB-LATE"], "status--warn")
    assert cells["JOB-RETRY"]["text"].startswith("▲ RETRY 1/3")
    assert by_class(cells["JOB-GONE"], "status--crit")


@requires_node
def test_jobs_awaiting_dev_gpu_line():
    """LANE0 section 4's own reading. It works from the ledger row alone --
    the parked job says so in ``last_error`` long before the queue directory
    exists -- and gets richer once the offload manifests are there."""
    parked = job(job_id="JOB-PARKED", state="pending", checkpoint=None, settled_ts=None, duration_s=None,
                 last_error="awaiting DEV GPU: offload ocr job queued", failure_class="environmental")
    panel = jobs_panel([parked], offload={"available": False, "awaiting": 1,
                                          "counts": {"pending": 0, "claimed": 0, "done": 0, "failed": 0},
                                          "jobs": {}})
    tree = run_harness("renderJobsCard", [panel, {"nowMs": NOW_MS, "snapshot": None}])["tree"]
    assert "AWAITING DEV GPU" in one(by_class(tree, "cell-state"), "state cell")["text"]
    assert one(by_class(tree, "cell-progress"), "progress")["text"] == "offload: not yet queued"
    assert "1 job waits for the DEV GPU — run the GPU worker" in tree["text"]

    panel["offload"] = {"available": True, "awaiting": 1,
                        "counts": {"pending": 0, "claimed": 1, "done": 0, "failed": 0},
                        "jobs": {"JOB-PARKED": {"state": "claimed", "worker_id": "DEV-1",
                                                "heartbeat_ts": "2026-09-05T01:59:30.000Z",
                                                "offload_attempts": 1}}}
    tree = run_harness("renderJobsCard", [panel, {"nowMs": NOW_MS, "snapshot": None}])["tree"]
    assert one(by_class(tree, "cell-progress"), "progress")["text"].startswith("claimed by DEV-1")


@requires_node
def test_jobs_deltas_against_previous_snapshot():
    """Free "what changed since you looked away" (the k9s per-cell deltas).
    The first render of a page must show NONE of them: a page that flags
    every row as new on load says nothing at all."""
    before = jobs_panel([
        job(job_id="J1", state="running", checkpoint=None, settled_ts=None, duration_s=None),
        job(job_id="J2", state="pending", attempts=1, checkpoint=None, settled_ts=None, duration_s=None),
    ])
    after = jobs_panel([
        job(job_id="J1", state="complete", checkpoint=None, duration_s=10),
        job(job_id="J2", state="running", attempts=2, checkpoint=None, settled_ts=None, duration_s=None),
        job(job_id="J3", state="running", checkpoint=None, settled_ts=None, duration_s=None),
    ])
    snapshot = run_harness("jobsSnapshotOf", [before])["value"]
    assert set(snapshot) == {"J1", "J2"}

    tree = run_harness("renderJobsCard", [
        after, {"nowMs": NOW_MS, "snapshot": snapshot, "keepSnapshot": True}])["tree"]
    rows = {one(by_class(r, "cell-id"), "id")["text"]: r for r in by_class(tree, "job-row")}
    assert one(by_class(rows["J3"], "cell-delta"), "delta")["text"] == "↑"
    assert "is-new" in rows["J3"]["classes"]
    assert one(by_class(rows["J1"], "cell-delta"), "delta")["text"] == "↓"
    assert one(by_class(rows["J2"], "cell-delta"), "delta")["text"] == "Δ"
    # attempts is displayed inside the STATE cell (`RETRY a/max`), so that is
    # the cell a change of either fact marks.
    assert "cell-changed" in one(by_class(rows["J2"], "cell-state"), "state")["classes"]

    tree = run_harness("renderJobsCard", [
        before, {"nowMs": NOW_MS, "snapshot": None, "keepSnapshot": True}])["tree"]
    assert [n["text"] for n in by_class(tree, "cell-delta")] == ["", ""]


@requires_node
def test_jobs_truncation_reports_itself():
    """House rule: truncation reports itself. The server caps ``recent_jobs``
    at 50; the state counts say how many rows the ledger really holds."""
    panel = jobs_panel([job(job_id=f"J{i}", checkpoint=None) for i in range(50)])
    panel["state_counts"] = {"complete": 80}
    tree = run_harness("renderJobsCard", [panel, {"nowMs": NOW_MS, "snapshot": None}])["tree"]
    assert "30 more not shown" in one(by_class(tree, "trunc-note"), "truncation note")["text"]


@requires_node
def test_jobs_empty_state_prints_zero_tally():
    tree = run_harness("renderJobsCard", [jobs_panel([]), {"nowMs": NOW_MS, "snapshot": None}])["tree"]
    assert "0 RUNNING" in tree["text"]
    assert "trialerror jobs enqueue" in tree["text"]


# ===========================================================================
# sweep tests 14-15 -- DIAGNOSTICS
# ===========================================================================


@requires_node
def test_diagnostics_order_and_tally():
    """Design ruling, amending the canvas: FAIL leads. The canvas copy said
    "warn first, then fail"; a failure must never sit below a long warn
    list."""
    checks = (
        [check(f"pass_{i}", "pass", category=f"cat{i % 19}") for i in range(30)]
        + [check(f"warn_{i}", "warn", category="obs") for i in range(5)]
        + [check(f"skip_{i}", "skip", category="gpu") for i in range(12)]
    )
    tree = run_harness("renderDiagnosticsCard", [doctor_panel(checks), {"nowMs": NOW_MS}])["tree"]
    tally = one(by_class(tree, "tally-row-inline"), "tally")
    # the tally reads in the same severity order as the rows below it: a card
    # whose summary and whose list disagree about what matters first is a card
    # you have to read twice.
    assert [s["text"] for s in by_class(tally, "status")] == [
        "✗ 0 FAILED", "▲ 5 WARNED", "○ 12 SKIPPED", "✓ 30 PASSED",
    ]

    visible = by_class(tree, "check-list")[0]
    first_five = [one(by_class(r, "name"), "name")["text"] for r in by_class(visible, "check-row")][:5]
    assert all(n.startswith("warn_") for n in first_five)

    details = one([d for d in by_tag(tree, "details")
                   if "PASS" in one(by_tag(d, "summary"), "summary")["text"]], "pass disclosure")
    assert "open" not in details["attrs"], "30 passing checks must not be the first thing on the card"

    mixed = doctor_panel([check("b_warn", "warn"), check("a_fail", "fail"), check("c_warn", "warn")])
    tree = run_harness("renderDiagnosticsCard", [mixed, {"nowMs": NOW_MS}])["tree"]
    names = [one(by_class(r, "name"), "name")["text"] for r in by_class(tree, "check-row")]
    assert names[0] == "a_fail"


@requires_node
def test_diagnostics_head_counts_checks_and_categories():
    """The canvas hard-codes "45 CHECKS, 17 CATEGORIES"; the real numbers are
    a property of the run this card is reporting, and are counted from it."""
    checks = [check(f"c{i}", "pass", category=f"cat{i % 19}") for i in range(47)]
    tree = run_harness("diagnosticsHead", [doctor_panel(checks)])["tree"]
    assert tree["text"] == "47 CHECKS, 19 CATEGORIES"


@requires_node
def test_card_heads_carry_the_one_glance_reading():
    """The head slot is where a card says what it is in one glance: the
    status the operator scans for before reading anything else."""
    assert run_harness("sessionHead", [session_panel()])["tree"]["text"] == "✓ ONE OPEN"
    assert run_harness("sessionHead", [session_panel(open_session=None)])["tree"]["text"] == "○ NONE OPEN"
    assert run_harness("sessionHead", [{"status": "invariant_violation"}])["tree"]["text"] \
        == "✗ INVARIANT VIOLATION"
    assert run_harness("ledgerHead", [budget_panel()])["tree"]["text"] == "○ 4 / 5 RECONCILED"

    jobs = jobs_panel([job(job_id="J1", state="running", checkpoint=None, settled_ts=None)],
                      stale_leases=[{"job_id": "J1"}])
    head = run_harness("jobsHead", [jobs])["tree"]
    assert "1 RUNNING" in head["text"]
    assert "STALE LEASES 1" in head["text"]
    assert by_class(head, "status--crit"), "an expired lease is the crit hue, not decoration"


@requires_node
def test_diagnostics_status_chips_and_category_text_filter():
    """Sweep §3.6: each tally entry is also the filter for its own status,
    and the category text narrows the list. The TALLY itself never moves --
    a summary that changes with the filter cannot be used to check it."""
    checks = [check("obs_exporter_reachable", "warn", category="obs"),
              check("xid_dangling", "pass", category="stores"),
              check("anchors_dangling", "pass", category="stores")]
    panel = doctor_panel(checks)
    opts = {"nowMs": NOW_MS, "onDoctorFilter": {"__fn": "filter"},
            "doctorFilter": {"status": "warn", "category": ""}}
    tree = run_harness("renderDiagnosticsCard", [panel, opts])["tree"]
    chips = by_class(tree, "tally-chip")
    assert len(chips) == 4
    assert [c["attrs"]["aria-pressed"] for c in chips] == ["false", "true", "false", "false"]
    assert "2 PASSED" in one(by_class(tree, "tally-row-inline"), "tally")["text"]
    assert [one(by_class(r, "name"), "name")["text"] for r in by_class(tree, "check-row")] \
        == ["obs_exporter_reachable"]
    assert "showing 1 of 3 checks" in tree["text"]

    opts["doctorFilter"] = {"status": None, "category": "stores"}
    tree = run_harness("renderDiagnosticsCard", [panel, opts])["tree"]
    assert len(by_class(tree, "check-row")) == 2
    assert one(by_class(tree, "check-filter"), "filter input")["attrs"]["value"] == "stores"

    # a snapshot has no callback to hand it, so the tally is a reading again
    tree = run_harness("renderDiagnosticsCard", [panel, {"nowMs": NOW_MS}])["tree"]
    assert not by_class(tree, "tally-chip")
    assert not by_class(tree, "check-filter")


@requires_node
def test_diagnostics_never_run_is_neutral():
    """console-8: an expected empty state must not alarm. The sweep is
    expensive and runs only when asked; saying so IS the reading."""
    tree = run_harness("renderDiagnosticsCard", [
        {"status": "never_run", "message": "doctor has not been run from this dashboard yet"},
        {"nowMs": NOW_MS, "onRunDoctor": {"__fn": "run"}},
    ])["tree"]
    assert by_class(tree, "callout--neutral")
    assert not by_class(tree, "warn-callout")
    button = one(by_tag(tree, "button"), "run button")
    assert button["text"] == "RUN THE CHECK SWEEP"
    assert button["listeners"] == ["click"]


@requires_node
def test_diagnostics_run_button_is_disabled_with_a_reason_in_a_snapshot():
    """A static export has no server to POST to. The verb is still drawn, and
    it still says why it cannot be used (12.11)."""
    tree = run_harness("renderDiagnosticsCard", [{"status": "never_run"}, {"nowMs": NOW_MS}])["tree"]
    button = one(by_tag(tree, "button"), "run button")
    assert button["attrs"]["disabled"] == "disabled"
    assert "no server" in button["attrs"]["title"]


# ===========================================================================
# sweep tests 16-18 -- shared primitives
# ===========================================================================


@requires_node
def test_tally_rows_vocab_and_null_sentinel():
    """console-9: every gate starts life with a NULL verdict, and the
    JSON-transport sentinel used to leak into the page as a row label."""
    tree = run_harness("tallyRows", [{"FAIL": 1, "PASS": 2, "__null__": 3}, "GATE_VERDICT"])["tree"]
    rows = by_class(tree, "tally-row")
    assert len(rows) == 3
    assert [one(by_class(r, "k"), "label")["text"] for r in rows] == ["FAIL", "PASS", "unset"]
    assert "status--crit" in rows[0]["classes"]
    assert "status--settled" in rows[1]["classes"]
    assert "status--pending" in rows[2]["classes"]
    assert "__null__" not in tree["text"]


@requires_node
def test_status_callout_tiers_and_payload_passthrough():
    """console-8 + M-P-2: the tier map, and the payload rendered underneath
    instead of thrown away -- which is how an ext panel's own block becomes
    visible at all."""
    tree = run_harness("statusCallout", [
        {"status": "awaiting_data", "message": "no rows yet", "demo": {"a": 1}}])["tree"]
    assert "callout--neutral" in tree["classes"]
    assert "demo" in tree["text"] and "a" in tree["text"]

    tree = run_harness("statusCallout", [{"status": "error", "message": "KeyError: x"}])["tree"]
    assert "warn-callout" in tree["classes"]

    tree = run_harness("statusCallout", [{"status": "invariant_violation", "message": "two open"}])["tree"]
    assert "callout--crit" in tree["classes"]


@requires_node
def test_fmt_ago_and_ticker_metadata():
    """A console for scanning cannot ask the operator to subtract ISO
    strings; and a node the tick cannot find again cannot be re-read."""
    tree = run_harness("fmtAgo", ["2026-09-05T01:58:30.000Z", {"nowMs": NOW_MS}])["tree"]
    assert tree["text"] == "1m ago"
    assert tree["attrs"]["title"] == "2026-09-05T01:58:30.000Z"
    assert tree["attrs"]["data-ts"] == "2026-09-05T01:58:30.000Z"

    tree = run_harness("fmtAgo", ["2026-09-05T04:10:00.000Z", {"nowMs": NOW_MS}])["tree"]
    assert tree["text"] == "in 2h 10m"


# ===========================================================================
# sweep tests 19-21 -- the idle-gap timeline
# ===========================================================================

TEN_HOURS = {"start_ts": "2026-09-05T00:00:00.000Z", "end_ts": "2026-09-05T10:00:00.000Z"}


@requires_node
def test_compress_idle_gaps_pure():
    """langfuse's idle-gap compression: collapse gaps wider than 5% of the
    span to a marked column so the busy parts get the pixels, and remap the
    coordinates BEFORE the view transform so the mapping stays monotonic."""
    layout = node_json(
        "return TE.compressIdleGaps(%s, %s, [], {nowMs: Date.parse('2026-09-05T10:00:00.000Z')});"
        % (json.dumps(TEN_HOURS), json.dumps([
            {"start_ts": "2026-09-05T00:00:00.000Z", "end_ts": "2026-09-05T00:10:00.000Z"},
            {"start_ts": "2026-09-05T09:43:20.000Z", "end_ts": "2026-09-05T10:00:00.000Z"},
        ]))
    )
    collapsed = [s for s in layout["segments"] if s["collapsed"]]
    assert len(collapsed) == 1
    assert collapsed[0]["label"] == "9h 33m idle"
    assert collapsed[0]["x1"] - collapsed[0]["x0"] == pytest.approx(14)

    # x(t) is fully determined by the segment list, so the one-second sweep
    # runs here rather than as 36,000 subprocesses.
    def x_of(t_ms: float) -> float:
        for s in layout["segments"]:
            if s["t0"] <= t_ms <= s["t1"]:
                if s["t1"] == s["t0"]:
                    return s["x0"]
                return s["x0"] + (s["x1"] - s["x0"]) * ((t_ms - s["t0"]) / (s["t1"] - s["t0"]))
        return layout["width"]

    previous = -1.0
    for second in range(0, 36001):
        value = x_of(layout["t0"] + second * 1000)
        assert value >= previous - 1e-9, f"x(t) went backwards at +{second}s"
        previous = value

    labels = [t["label"] for t in layout["ticks"]]
    assert labels[0] == "00:00" and labels[-1] == "10:00"
    assert "09:43" in labels, "a collapsed marker must say where the time went"


@requires_node
def test_compress_idle_gaps_threshold_is_five_percent_with_a_sixty_second_floor():
    four_percent = node_json(
        "return TE.compressIdleGaps(%s, %s, [], {nowMs: 0}).collapsedCount;"
        % (json.dumps(TEN_HOURS), json.dumps([
            {"start_ts": "2026-09-05T00:00:00.000Z", "end_ts": "2026-09-05T00:10:00.000Z"},
            {"start_ts": "2026-09-05T00:34:00.000Z", "end_ts": "2026-09-05T10:00:00.000Z"},
        ]))
    )
    assert four_percent == 0, "a 24-minute gap in a 10-hour window is 4% and stays linear"

    layout = node_json(
        "return TE.compressIdleGaps({start_ts:'2026-09-05T00:00:00.000Z',"
        "end_ts:'2026-09-05T00:10:00.000Z'}, [], [], {nowMs: 0});"
    )
    assert layout["thresholdMs"] == 60000, "5% of ten minutes is 30 s; the floor is a minute"
    # ...and a window with nothing in it at all is drawn linearly. Collapsing
    # makes room for the busy parts; with nothing busy there is nothing to
    # make room FOR, and a whole session squeezed into a 14-unit marker says
    # "all idle" in the least readable way available.
    assert layout["collapsedCount"] == 0


@requires_node
def test_timeline_render_lanes_and_legend():
    spans = [
        {"id": "L1", "kind": "launch", "lane": "implementer", "label": "lane c",
         "start_ts": "2026-09-05T01:00:00.000Z", "end_ts": None, "status": "running",
         "ref": {"launch_id": "L1"}},
        {"id": "J1", "kind": "job", "lane": "embed · DOC-1", "label": "DOC-1",
         "start_ts": "2026-09-05T00:10:00.000Z", "end_ts": "2026-09-05T00:20:00.000Z",
         "status": "retried", "ref": {"job_id": "J1"}},
        {"id": "R1", "kind": "room", "lane": "room ROOM-1", "label": "does it hold?",
         "start_ts": "2026-09-05T00:30:00.000Z", "end_ts": "2026-09-05T00:40:00.000Z",
         "status": "frozen", "ref": {"room_id": "R1"}},
    ]
    tree = run_harness("renderTimelineCard", [timeline_fixture(spans), {"nowMs": NOW_MS}])["tree"]
    assert len(by_class(tree, "lane")) == 3
    running = one(by_class(tree, "bar--running"), "running bar")
    assert "pulse" in running["classes"]
    # the window is open, so the running bar ends at now -- which IS the right
    # edge of the layout
    assert float(running["attrs"]["x"]) + float(running["attrs"]["width"]) == pytest.approx(1000, abs=0.5)
    assert by_class(tree, "bar--retried")
    legend = one(by_class(tree, "timeline-legend"), "legend")
    for entry in ("RUNNING NOW", "COMPLETED", "RETRIED", "COLLAPSED IDLE"):
        assert entry in legend["text"]

    many = [dict(spans[1], id=f"J{i}", lane=f"embed · DOC-{i}") for i in range(30)]
    tree = run_harness("renderTimelineCard", [timeline_fixture(many), {"nowMs": NOW_MS}])["tree"]
    assert len(by_class(tree, "lane")) == 24
    assert "6 more lanes not shown" in one(by_class(tree, "trunc-note"), "lane truncation")["text"]


@requires_node
def test_timeline_empty_states():
    tree = run_harness("renderTimelineCard", [None, {"nowMs": NOW_MS}])["tree"]
    assert tree["text"].startswith("no open session")

    tree = run_harness("renderTimelineCard", [timeline_fixture([]), {"nowMs": NOW_MS}])["tree"]
    assert by_class(tree, "timeline-axis"), "the axis is drawn so the operator sees the window exists"
    assert "nothing launched yet" in tree["text"]


# ===========================================================================
# sweep test 22 -- the health matrix
# ===========================================================================


@requires_node
def test_compute_health_matrix():
    """console-2, as a matrix rather than a source grep: the badge read OK in
    the most common bad state this harness has."""
    cases = {
        "clean": ({}, "ok", "OK"),
        "cannot_close": ({"session": {"status": "ok", "open_session": {
            "close_readiness": {"ready": False, "problems": []}}}}, "warn", "1 WARN"),
        "invariant": ({"session": {"status": "invariant_violation", "message": "two open"}},
                      "crit", "1 CRIT"),
        "stale_lease": ({"jobs": {"status": "ok", "stale_leases": [{"job_id": "J"}]}}, "crit", "1 CRIT"),
        "stale_quota": ({"budget": {"status": "ok",
                                    "plan_quota": {"available": True, "fresh": False}}}, "warn", "1 WARN"),
        "panel_error": ({"corpus": {"status": "error", "message": "KeyError"}}, "crit", "1 CRIT"),
        "awaiting_gpu": ({"jobs": {"status": "ok", "offload": {"awaiting": 2}}}, "warn", "1 WARN"),
        "over_hard": ({"budget": {"status": "ok", "accounts": [
            {"budget_status": {"pools": [{"pool_id": "P", "over_hard": True}]}}]}}, "crit", "1 CRIT"),
    }
    result = node_json(
        "const cases = %s; const out = {};"
        "Object.keys(cases).forEach(k => { const h = TE.computeHealth(cases[k]); out[k] = [h.level, h.label]; });"
        "return out;" % json.dumps({k: v[0] for k, v in cases.items()})
    )
    for name, (_panels, level, label) in cases.items():
        assert result[name] == [level, label], name


# ===========================================================================
# sweep test 23 -- GATES and CORPUS
# ===========================================================================


@requires_node
def test_gates_and_corpus_compact_cards():
    """Neither card is on the canvas -- S9 moved gates to Dossier and corpus
    counts to Home -- but operators use both from here, so they stay as
    compact readings rather than being deleted out from under them."""
    gates = {
        "status": "ok",
        "pending_edits": [{"gate_id": "GATE-1", "artifact_id": "ART-1", "title": "the draft",
                           "state": "gated", "unverified_count": 1,
                           "edits": [{"edit_id": "E1", "verified": True},
                                     {"edit_id": "E2", "verified": False}]}],
        "gate_state_counts": {"gated": 1}, "gate_verdict_counts": {"__null__": 1},
        "reproduction_status_counts": {"unrun": 1},
    }
    tree = run_harness("renderGatesCard", [gates, {"nowMs": NOW_MS}])["tree"]
    assert "1 unverified" in reading(tree, "EDITS")["text"]
    assert "status--warn" in reading(tree, "EDITS")["classes"]

    corpus = {
        "status": "ok",
        "counts": {"sources": 131, "documents": 130, "chunks": 15000, "quote_anchors": 42},
        "license_tier_counts": {"open": 100},
        "summary_coverage": {"documents_with_current_summary": 65, "total_documents": 130},
        "extract_coverage": {"pending_records": 4},
        "stale_anchors": 0,
    }
    tree = run_harness("renderCorpusCard", [corpus, {"nowMs": NOW_MS}])["tree"]
    row = reading(tree, "STALE ANCHORS")
    assert row["text"].endswith("STALE ANCHORS 0")
    assert "status--settled" in row["classes"]
    assert "4 candidates await accept/reject" in reading(tree, "EXTRACTION QUEUE")["text"]
    assert "status--warn" in reading(tree, "EXTRACTION QUEUE")["classes"]

    corpus["stale_anchors"] = 3
    tree = run_harness("renderCorpusCard", [corpus, {"nowMs": NOW_MS}])["tree"]
    assert "status--crit" in reading(tree, "STALE ANCHORS")["classes"]


# ---------------------------------------------------------------------------
# the seam between the two files
# ---------------------------------------------------------------------------

DASHBOARD_HTML = REPO_ROOT / "trialerror" / "dashboard" / "static" / "dashboard.html"


def test_the_render_file_loads_before_the_script_that_uses_it():
    """``window.TEConsole`` is read at start-up by the inline script. A tag
    placed after it (or given ``defer``) leaves TECONSOLE null on every load,
    which looks exactly like "the Console renderer is not finished yet"."""
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    tag = '<script src="console_render.js"></script>'
    assert tag in html
    assert html.index(tag) < html.index("<script>\n(function ()"), \
        "console_render.js must be parsed before the inline script, not after it"
    assert html.index(tag) < html.index("window.TEConsole.create")


def test_every_console_card_hook_is_real_markup_and_every_container_is_claimed():
    """Both directions of the card wiring, in one place: a hook the script
    reads must exist in the markup (or the card silently never paints), and a
    container in the markup must be claimed by the script (or it sits at
    "loading..." forever). Both failures are invisible in a browser.

    K4 gave every card a second hook -- the head slot where its own status
    reading goes -- so the check covers both families."""
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    bodies = set(re.findall(r'data-role="console-body-([a-z]+)"', html))
    heads = set(re.findall(r'data-role="console-head-([a-z]+)"', html))
    assert bodies == heads, {"body only": bodies - heads, "head only": heads - bodies}
    assert len(bodies) == 8, sorted(bodies)

    targets_block = html[html.index("function consoleTargets()"):]
    targets_block = targets_block[: targets_block.index("\n  }\n")]
    claimed = set(re.findall(r'pair\("([a-z]+)"\)', targets_block))
    assert claimed == bodies, {"only in script": claimed - bodies, "only in markup": bodies - claimed}


def test_the_page_passes_a_namespaced_helper_for_the_timeline():
    """An <svg> built by the HTML factory is an HTMLUnknownElement: it parses,
    it sits in the tree, and it draws nothing. The timeline is the only thing
    on the page that needs the SVG namespace, and this is where it gets it."""
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    assert "createElementNS(SVG_NS, tag)" in html
    assert "window.TEConsole.create({ h: el, hs: svgEl })" in html


def test_the_one_second_tick_belongs_to_the_console_and_is_cleared_on_the_way_out():
    """console-7 / LU-11. Once LU-7's accidental 3-second repaint was cut,
    every age on the page would have frozen between real changes -- and a
    timer that survives leaving the panel is a leak per visit."""
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    assert "function tickAges()" in html
    assert "startConsoleTick();" in html
    assert 'if (name !== "console") stopConsoleTick();' in html
