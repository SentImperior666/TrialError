"""Batch C2 of the 2026-09 dashboard bug sweep -- client resilience and
correctness in the served page's inline script.

WHY THIS FILE IS SHAPED LIKE THIS. The findings in C2 (LU-2, LU-3, LU-5/6,
LU-8, LU-12, P-1, M-P-1, M-AC-1, M-AC-3, M-P-3, M-LU-7, VA-3, VA-4, AC-1,
AC-2, console-2, M-CON-2) all live in ``static/dashboard.html``'s inline
script, and this repo has no JS execution harness yet: the sweep's own
``tests/_dom_shim.js`` + ``_console_render_harness.js`` land in step C4, and
adding Node to the dependency surface early is out of this step's scope
(sweep §3.11's gate for C2 says "test 22 via the Node harness once it
exists -- else a temporary Python port of the matrix"). Two kinds of test
are available in the meantime, and both are here because neither alone is
worth much:

1. **Source invariants** over the shipped static files -- the behaviour each
   finding demanded, stated as something a future edit cannot quietly undo
   (a chip write that skips ``data-state``; an empty ``.catch``; an
   unguarded ``agreement_pct.toFixed``). These are structural, and they say
   so; they do not claim to prove the page renders.
2. **Payload contracts** -- the fields those client behaviours read, built
   for real out of ``trialerror.dashboard.data`` against a seeded store.
   This is the half that actually rots: the client can only branch on
   ``agreement_pct is None`` or derive a failed gate's last step from
   ``gate_history[].from_state`` if the builders really emit them, and a
   server-side rename would otherwise break the page with every test green.

The rendered result is the orchestrator's browser pass:
``DASHBOARD_LIVE_ITEMS["live_dom_client_resilience"]`` (skip-marked in
``tests/test_dashboard_accept_items.py``) names the exact steps, which are
checklist items 1-2 of the lane C spec plus the AC-1 repro.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from trialerror.artifacts.gates import open_gate, record_verdict, submit_gate
from trialerror.artifacts.registry import create_artifact
from trialerror.dashboard import data
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.rooms.api import create_room, score_dp
from trialerror.stores.store import open_store
from tests._store_fixtures import populate_one_of_everything

STATIC_DIR = Path(__file__).resolve().parents[1] / "trialerror" / "dashboard" / "static"
HTML = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
#: K4 lifted `computeHealth` out of the inline script and into the renderer
#: file, where the Node harness can run its whole matrix without a browser
#: (sweep test 22). The source assertions below follow it; `setHealth`, the
#: half that touches the page, stays in dashboard.html.
CONSOLE_RENDER = (STATIC_DIR / "console_render.js").read_text(encoding="utf-8")


def _fn_body(name: str, source: str = HTML) -> str:
    """The text of one top-level ``function <name>(...) { ... }``, brace
    matched. Good enough for this file (the inline script has no braces
    inside string literals at these sites) and far less brittle than the
    line numbers the sweep's findings were written against."""
    start = source.index("function " + name + "(")
    open_brace = source.index("{", start)
    depth, i = 0, open_brace
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces in function {name}")


def _listener_body(event: str) -> str:
    """The body of ``es.addEventListener("<event>", function (ev) { ... })``."""
    marker = 'es.addEventListener("' + event + '"'
    start = HTML.index(marker)
    open_brace = HTML.index("{", HTML.index("function", start))
    depth, i = 0, open_brace
    while i < len(HTML):
        if HTML[i] == "{":
            depth += 1
        elif HTML[i] == "}":
            depth -= 1
            if depth == 0:
                return HTML[open_brace : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces in the {event} listener")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def seeded(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    store.close()
    rostore = open_store_ro(program_root, platform_root=platform_root)
    yield rostore, ids
    rostore.close()


# ===========================================================================
# VA-3 + LU-8 + M-LU-7 -- the topbar is the only freshness signal
# ===========================================================================
def test_mode_chip_declares_a_state_and_announces_changes():
    """VA-3: the chip ships with ``data-state`` and ``aria-live`` in the
    markup, so the CSS rule below has something to match before any JS
    runs and a screen reader hears the transition."""
    chip = re.search(r'<span class="topbar-field" data-role="topbar-mode"[^>]*>', HTML)
    assert chip is not None, "the topbar mode chip is gone"
    assert 'data-state="static"' in chip.group(0)
    assert 'aria-live="polite"' in chip.group(0)


def test_mode_chip_has_one_hue_per_state_in_css():
    """VA-3's fix, corrected: colour keyed off ``data-state``, three states,
    each on a semantic token rather than a literal."""
    for chip_state, token in (("live", "--live"), ("reconnecting", "--warn"), ("static", "--ink-3")):
        rule = re.search(r'\.topbar-field\[data-state="' + chip_state + r'"\]\s*\{([^}]*)\}', CSS)
        assert rule is not None, f"no CSS rule for the {chip_state} chip state"
        assert token in rule.group(1), f"the {chip_state} chip must use var({token})"


def test_manual_is_not_a_chip_state_anymore():
    """LU-8: "manual" was a label with no behaviour behind it and is not
    one of the states this dashboard's own acceptance item enumerates. It
    may survive in the comment that explains its removal, never in code."""
    for match in re.finditer(r'"manual"', HTML):
        line_start = HTML.rfind("\n", 0, match.start()) + 1
        line = HTML[line_start : HTML.index("\n", match.start())]
        assert line.lstrip().startswith(("*", "//", "<!--")), f"live use of the manual chip state: {line.strip()}"


def test_every_chip_write_goes_through_render_chip():
    """VA-3's root cause: the SSE error path set the chip's text and not
    its ``data-state``, so the colour rule could never match on the one
    path that needed it. One writer, and it always sets both."""
    selector = '[data-role="topbar-mode"]'
    chip_fn = _fn_body("renderChip")
    assert HTML.count(selector) == 1, (
        "the chip selector appears " + str(HTML.count(selector)) + " times; every write must go "
        "through renderChip so text and data-state can never disagree"
    )
    assert selector in chip_fn
    assert 'setAttribute("data-state"' in chip_fn
    assert "textContent" in chip_fn


def test_topbar_carries_a_data_as_of_reading():
    """M-LU-7: the chip reports the SOCKET; nothing reported how old the
    numbers were. A live socket over three-hour-old numbers is the state
    the pair exists to make unmisreadable."""
    assert 'data-role="topbar-asof"' in HTML
    as_of = _fn_body("setAsOf")
    assert "generated_ts" in as_of
    assert 'setAttribute("title"' in as_of, "the absolute timestamp must survive as a title"
    assert "setAsOf(meta)" in _fn_body("setTopbar")


def test_meta_really_carries_generated_ts_on_both_paths():
    """The payload contract behind the reading above: `serve.build_meta`
    and the static exporter both stamp it, so `AS OF` is never a dash on a
    page that has data."""
    import inspect

    from trialerror.dashboard import export as dashboard_export
    from trialerror.dashboard import serve as dashboard_serve

    assert '"generated_ts"' in inspect.getsource(dashboard_serve.build_meta)
    assert '"generated_ts"' in inspect.getsource(dashboard_export.build_snapshot_html)


# ===========================================================================
# LU-5 / LU-6 / M-AC-3 / M-P-3 -- one fetch-error surface, nothing swallowed
# ===========================================================================
LOADERS = [
    ("loadHome", "home"),
    ("loadFeed", "feed"),
    ("loadRooms", "rooms"),
    ("loadDeterminations", "determinations"),
    ("loadDossier", "dossier"),
    ("loadLexicon", "lexicon"),
    ("loadCourse", "course"),
    ("loadConsole", "console"),
    ("loadExt", "ext-"),
]


@pytest.mark.parametrize("fn_name,panel", LOADERS)
def test_every_panel_loader_routes_failures_to_the_shared_surface(fn_name, panel):
    """LU-5: eight loaders, four ``.catch`` sites, three of them empty --
    a rejected fetch left "loading…" on screen under a "live" chip."""
    body = _fn_body(fn_name)
    assert "guardPanelLoad(" in body, f"{fn_name} does not guard its fetch"
    assert panel in body, f"{fn_name} guards under the wrong panel name"


def test_search_failures_are_visible_too():
    """M-P-3: ``do_GET`` wraps ``build_search`` in no try/except, so a query
    the engine cannot answer closes the socket with no response -- and the
    previous result list stayed on screen as the answer to the new
    question. (The server half is W3/P-2.)"""
    assert "guardPanelLoad(\"search\"" in _fn_body("doSearch")


def test_the_error_surface_offers_a_retry_and_keeps_the_stack():
    show = _fn_body("showPanelFetchError")
    assert 'text: "RETRY"' in show
    assert "console.error(" in show, "the surface must not be the only record of the failure"
    assert "insertBefore" in show, "the banner sits above the stale content, not instead of it"
    assert ".panel-fetch-error" in CSS


def test_no_empty_catch_blocks_survive_in_the_page():
    """LU-6 generalised: an empty catch is how every one of these findings
    got to be invisible in the first place."""
    empties = re.findall(r"\.catch\(function\s*\([^)]*\)\s*\{\s*\}\s*\)", HTML)
    assert empties == [], f"{len(empties)} empty catch block(s) remain"


# ===========================================================================
# LU-2 / LU-3 / LU-12 / LU-8 -- reconnect, resync, retry
# ===========================================================================
def test_a_second_hello_resyncs_the_page():
    """LU-2: a server restart or a laptop wake re-established the stream
    and left every number frozen at whatever it was before the gap, still
    labelled live. The FIRST hello is this page's own connect and needs no
    refetch; every later one does."""
    hello = _listener_body("hello")
    assert "state.hadHello" in hello
    assert re.search(r"if\s*\(state\.hadHello\)\s*resync\(\)", hello), "a later hello must refetch"
    assert re.search(r"state\.hadHello\s*=\s*true", hello)
    assert 'setConn("live")' in hello


def test_changed_fetches_the_bundle_exactly_once():
    """LU-12: the handler fetched /all for the health badge and then called
    ``loadPanelContent``, which fetched /all again in the same millisecond
    on Home and Console."""
    changed = _listener_body("changed")
    assert changed.count("resync()") == 1
    assert "fetchAll(" not in changed, "the changed handler must not fetch on its own"
    resync_from = _fn_body("resyncFromBundle")
    assert "renderHome(bundle)" in resync_from and "renderConsoleFromBundle(bundle)" in resync_from
    assert "loadPanelContent(state.activePanel)" in resync_from, "panels that do not read the bundle still reload"


def test_changed_can_skip_a_platform_only_tick():
    """The client half of LU-7's fix: platform.db is shared by every program
    on the machine, so a launch booked under another program ticks this
    stream. Only Home and Console read platform rows."""
    changed = _listener_body("changed")
    assert "changed_stores" in changed
    assert '"platform"' in changed
    assert '"home"' in changed and '"console"' in changed


def test_the_boot_fetch_retries_with_capped_exponential_backoff():
    """LU-3 (blocker): one failed /all -- a program pointed at a missing
    platform.db was enough -- left a permanently blank page under a "live"
    label, with no retry anywhere in the file."""
    assert "var RETRY_BASE_MS = 1000;" in HTML
    assert "var RETRY_CAP_MS = 30000;" in HTML
    boot = _fn_body("bootFetch")
    assert "Math.min(delayMs * 2, RETRY_CAP_MS)" in boot
    assert "setTimeout(" in boot
    assert 'setConn("reconnecting")' in boot
    assert "showPanelFetchError(" in boot, "the operator gets a RETRY control, not just a wait"


def test_a_dead_stream_falls_back_to_thirty_second_polling():
    """LU-8: after MAX_CONSECUTIVE_ERRORS the page stops assuming the stream
    will return on its own; the loop stops the moment one does."""
    assert "var POLL_FALLBACK_MS = 30000;" in HTML
    assert "var MAX_CONSECUTIVE_ERRORS = 6;" in HTML
    start = _fn_body("startPollingFallback")
    assert "POLL_FALLBACK_MS" in start and "fetchAll(" in start
    for event in ("hello", "changed"):
        assert "stopPollingFallback()" in _listener_body(event), f"{event} must stop the fallback"


def test_the_stream_is_never_stacked_and_is_revived_when_closed():
    """A killed and restarted server is the checklist's item 2. EventSource
    reconnects itself from CONNECTING; a CLOSED stream is the browser
    giving up and nothing but a new EventSource revives it."""
    connect = _fn_body("connectSSE")
    assert "if (state.eventSource) return;" in connect
    assert "es.readyState === 2" in connect
    assert "state.eventSource = null;" in connect


# ===========================================================================
# P-1 / M-P-1 -- ext panels survive a deep link and a reconnect
# ===========================================================================
def test_ext_hashes_are_accepted_and_a_bad_hash_is_corrected():
    """P-1: reloading on an ext panel dropped the operator on Home with the
    ext hash still in the address bar -- which then lied again on the next
    reload."""
    fn = _fn_body("initialPanelFromHash")
    assert 'h.indexOf("ext-") === 0' in fn
    assert "ext_panels" in fn
    assert 'history.replaceState(null, "", "#home")' in fn, "a hash that names nothing must be corrected"


def test_rebuilding_the_ext_nav_re_applies_the_active_panel():
    """M-P-1: ``buildExtNav`` runs on every ``hello`` -- i.e. every
    reconnect -- and tears down every ext <section>, so an operator reading
    one was left with an empty content area under a "live" chip."""
    nav = _fn_body("buildExtNav")
    assert "applyActivePanel()" in nav
    assert 'state.activePanel.indexOf("ext-") === 0' in nav
    assert "applyActivePanel()" in _fn_body("switchPanel"), "one writer for the active-panel classes"


# ===========================================================================
# M-AC-1 -- a static snapshot says what it holds
# ===========================================================================
def test_static_selection_mismatch_is_reported_not_silently_reassigned():
    fn = _fn_body("staticSelectionMismatch")
    assert '"static_selection_unavailable"' in fn
    assert "STATIC_SELECTION_KEYS" in HTML


def test_the_static_selection_keys_match_what_the_builders_emit(seeded):
    """The contract half. The client compares the requested id against the
    panel's own baked ``active_*_id``; if a builder renames that field the
    comparison silently stops firing and M-AC-1 comes straight back."""
    rostore, _ids = seeded
    declared = re.search(r"var STATIC_SELECTION_KEYS = \{(.*?)\};", HTML, re.S).group(1)
    pairs = {
        panel: active_key
        for panel, _query_key, active_key in re.findall(r'(\w+):\s*\["(\w+)",\s*"(\w+)"\]', declared)
    }
    assert pairs == {
        "feed": "active_thread_id",
        "rooms": "active_room_id",
        "dossier": "active_artifact_id",
        # C6. Only the claim_id form is checkable this way: a snapshot bakes
        # `active_claim_id`, while an anchor_id/chunk_id request has no baked
        # field to compare against -- the builder's own `not_found` reading
        # covers that one.
        "evidence": "active_claim_id",
    }
    builders = {
        "feed": data.build_feed_panel,
        "rooms": data.build_rooms_panel,
        "dossier": data.build_dossier_panel,
        "evidence": data.build_evidence_panel,
    }
    for panel_name, active_key in pairs.items():
        panel = builders[panel_name](rostore)
        assert active_key in panel, f"{panel_name} panel no longer carries {active_key}"


# ===========================================================================
# AC-1 (blocker) -- an unscored discussion point is a reading, not a crash
# ===========================================================================
def test_agreement_pct_is_never_dereferenced_unguarded():
    """AC-1: ``check_room_converged`` returns ``agreement_pct: null`` for
    every DP nobody has scored, which is EVERY room from creation until the
    last DP is scored. ``null.toFixed`` threw out of renderRooms before the
    SCORE / FREEZE / EXPORT block was appended -- so the only in-UI way to
    leave the unscored state vanished exactly while the room was in it."""
    hits = list(re.finditer(r"[\w.]*agreement_pct\.toFixed\(", HTML))
    assert hits, "the percentage readings are gone entirely -- check this test, not just the page"
    for m in hits:
        window = HTML[max(0, m.start() - 500) : m.start()]
        assert ("agreement_pct === null" in window) or ("hasPct" in window), (
            "unguarded agreement_pct dereference at offset " + str(m.start())
        )
    assert "scored.agreement_pct.toFixed(" not in HTML, "the DP-ladder site must go through hasPct"
    assert 'statusEl("notscored", "NOT SCORED")' in HTML, "the null case needs a reading of its own"


def test_an_unscored_dp_really_reaches_the_rooms_panel_as_null(program_root, platform_root):
    """The payload half of AC-1, reproduced the way the finder did: a room
    with two discussion points, one scored. The panel the browser receives
    carries a null, and the SCORE affordance depends on the client
    surviving it."""
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    room = create_room(
        store,
        topic="AC-1 repro: two discussion points, one scored",
        discussion_points=[{"dp_id": "dp-1", "prompt": "first"}, {"dp_id": "dp-2", "prompt": "second"}],
        participants=["lens-a", "lens-b", "lens-c"],
        by_launch=ids["launch"],
    )
    score_dp(
        store,
        room_id=room["room_id"],
        dp_id="dp-1",
        judge=lambda _dp: 91.0,
        by_launch=ids["launch"],
    )
    store.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = data.build_rooms_panel(rostore, room_id=room["room_id"])
    finally:
        rostore.close()

    assert panel["status"] == "ok"
    per_dp = {d["dp_id"]: d for d in panel["convergence"]["per_dp"]}
    assert per_dp["dp-1"]["agreement_pct"] == pytest.approx(91.0)
    assert per_dp["dp-2"]["agreement_pct"] is None, "the null the client must branch on is gone"
    assert panel["convergence"]["all_scored"] is False


# ===========================================================================
# AC-2 -- `failed` is the gate track's exit, not its last step
# ===========================================================================
def test_gate_steps_do_not_contain_failed_and_the_exit_is_drawn_separately():
    assert 'var GATE_STEPS = ["draft", "submitted", "gated", "union_applied", "registered"];' in HTML
    dossier = _fn_body("renderDossier")
    assert 'p.gate.state === "failed"' in dossier
    assert 'gate_history' in dossier and 't.to_state === "failed"' in dossier
    assert 'text: "✗ FAILED"' in dossier
    assert ".gate-track .step--failed" in CSS


def test_a_failed_gate_ships_the_transition_the_client_walks_back_to(program_root, platform_root):
    """The payload half of AC-2: the client derives the step the gate
    actually reached from ``gate_history``'s hop into ``failed``. If the
    dossier builder stops shipping that list, the track silently falls back
    to "draft" for every failed gate."""
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    artifact = create_artifact(
        store, type_key=ids["template"], title="AC-2 failed gate", path="artifacts/ac2.md",
        sha256="b" * 64, by_launch=ids["launch"], purpose="AC-2 regression fixture",
    )
    gate = open_gate(store, artifact_id=artifact["artifact_id"])
    submit_gate(store, gate_id=gate["gate_id"], by_launch=ids["launch"])
    record_verdict(
        store, gate_id=gate["gate_id"], verdict="FAIL", critic_launch=ids["launch"],
        edits=[{"text": "not reproducible", "blocking": True}],
    )
    store.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = data.build_dossier_panel(rostore, artifact_id=artifact["artifact_id"])
    finally:
        rostore.close()

    assert panel["gate"]["state"] == "failed"
    hops = [t for t in panel["gate_history"] if t["to_state"] == "failed"]
    assert hops, "no gate_transition records the hop into failed"
    assert hops[-1]["from_state"] in ("draft", "submitted", "gated")
    # and the client's own step vocabulary can place it
    assert hops[-1]["from_state"] in json.loads(
        re.search(r"var GATE_STEPS = (\[[^\]]*\]);", HTML).group(1)
    )


# ===========================================================================
# console-2 + M-CON-2 -- the rail badge over the whole bundle (sweep test 22)
# ===========================================================================
HEALTH_CRIT_INPUTS = [
    'session.status === "invariant_violation"',
    "dangling_bookings",
    "stale_leases",
    "stale_anchors",
    "over_hard",
    'p.status === "error"',
]
HEALTH_WARN_INPUTS = [
    "readiness.ready === false",
    "pending_edits",
    "over_soft",
    "quota.available && !quota.fresh",
]


@pytest.mark.parametrize("needle", HEALTH_CRIT_INPUTS + HEALTH_WARN_INPUTS)
def test_compute_health_reads_every_input_the_spec_names(needle):
    """console-2 (blocker): the badge read OK in the most common bad state
    this harness has -- an open session that cannot close with PROVISIONAL
    launches under it. Input set is sweep §3.7. ``computeHealth`` is pure
    over the bundle's ``panels``, and K4 moved it into ``console_render.js``
    so the Node harness runs the real matrix (sweep test 22,
    ``tests/test_dashboard_console_render.py``); this stays as the source-side
    guard that no input is quietly dropped."""
    assert needle in _fn_body("computeHealth", CONSOLE_RENDER)


def test_compute_health_labels_count_contributing_items():
    fn = _fn_body("computeHealth", CONSOLE_RENDER)
    assert 'crit.length + " CRIT"' in fn
    assert 'warn.length + " WARN"' in fn
    assert '"OK"' in fn
    set_health = _fn_body("setHealth")
    assert "computeHealth(panels)" in set_health
    assert 'setAttribute("title"' in set_health, "the badge must list what it is counting"


def test_health_without_the_renderer_file_is_not_ok():
    """The delegation's fallback. A page whose renderer file failed to load
    cannot compute its own health, and "OK" is the one answer that would be
    a lie -- the same lie console-2 was."""
    fn = _fn_body("computeHealth")
    assert "TECONSOLE.computeHealth(panels)" in fn
    assert '"OK"' not in fn
    assert "HEALTH UNKNOWN" in fn


def test_the_doctor_run_flow_recomputes_health(seeded):
    """M-CON-2: a sweep that just turned up a FAIL has to move the badge;
    the old handler repainted one card and left health reading whatever it
    read before the run. K4 made the button part of the DIAGNOSTICS card,
    which re-draws on every render, so the handler became a named function
    the renderer is handed rather than a listener bound once to a node."""
    handler = _fn_body("runDoctorSweep")
    assert "loadConsole();" in handler
    assert "showPanelFetchError(" in handler, "LU-6: the empty catch here swallowed a rotated token"
    assert 'data-role="doctor-run-btn"' not in HTML, \
        "the subbar button is gone; the card head draws it now"


def test_the_health_inputs_exist_on_a_real_bundle(seeded):
    """The contract half of the matrix: every field ``computeHealth``
    branches on is really produced by its builder, so a server-side rename
    fails here instead of silently zeroing the badge."""
    rostore, _ids = seeded
    budget = data.build_budget_panel(rostore)
    assert "dangling_bookings" in budget
    assert {"available", "fresh"} <= set(budget["plan_quota"])
    for account in budget["accounts"]:
        for pool in account["budget_status"]["pools"]:
            assert {"over_soft", "over_hard", "pool_id"} <= set(pool)

    jobs = data.build_jobs_panel(rostore)
    assert "stale_leases" in jobs

    corpus = data.build_corpus_panel(rostore)
    assert "stale_anchors" in corpus

    gates = data.build_gates_panel(rostore)
    assert "pending_edits" in gates

    session = data.build_session_panel(rostore)
    assert "close_readiness" in session["open_session"]
    assert "ready" in session["open_session"]["close_readiness"]


# ===========================================================================
# VA-4 -- the course criterion label has a recovery path
# ===========================================================================
def test_course_criterion_labels_carry_their_full_text():
    """VA-4: ``.ladder-row .name`` is capped at 168px with ellipsis and the
    labels run to several hundred, on the one surface that carries the
    research course."""
    course = _fn_body("renderCourse")
    assert re.search(r'"class":\s*"name",\s*title:\s*c\.label,\s*text:\s*c\.label', course)


# ===========================================================================
# W3 / P-2 -- the client half of the write-path batch
#
# Same two kinds of test as everything above, for the same reason: these
# behaviours live in the inline script, and the Node harness lands in C4.
# The rendered result is the orchestrator's browser pass (spec §6 checklist
# items 4 and 7).
# ===========================================================================
def test_wire_write_action_repaints_the_panel_on_failure_too():
    """WA-2. The `finally` re-enables the button from `writesEnabled()`
    alone, which answers "can this page write at all" -- not "is this
    button still a legal thing to press". After the commonest refusal there
    now is (WA-1's own: the room is ALREADY frozen) that is exactly wrong,
    so a caller's panel loader has to run on the failure path as well."""
    body = _fn_body("wireWriteAction")
    assert "onFailure" in body, "wireWriteAction takes no failure hook"
    assert re.search(r"if\s*\(failed\s*&&\s*onFailure\)\s*onFailure\(\)", body)
    # the flag is set on BOTH failure arms: a business refusal and a
    # rejected promise.
    assert body.count("failed = true") == 2


def _call_text(source: str, start: int) -> str:
    """The full ``name(...)`` text of the call beginning at ``start``."""
    i = source.index("(", start)
    depth = 0
    while i < len(source):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError("unbalanced call")


def _call_arity(source: str, start: int) -> int:
    """How many top-level arguments the call at ``start`` passes. Commas
    inside nested parens/braces/brackets do not count."""
    text = _call_text(source, start)
    # `//` comments inside the argument list carry prose, and prose has
    # commas in it. None of these call sites has `//` inside a string.
    text = "\n".join(line.split("//")[0] for line in text.splitlines())
    depth, args = 0, 1
    for ch in text[text.index("(") :]:
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        elif ch == "," and depth == 1:
            args += 1
    return args


def test_every_write_action_call_site_passes_a_failure_repaint():
    """A hook nothing passes is a hook that does not exist: every
    ``wireWriteAction(`` call outside the definition must supply the eighth
    argument, and it must be a real panel loader."""
    calls = [m.start() for m in re.finditer(r"\bwireWriteAction\(", HTML)]
    assert len(calls) >= 10  # the first hit is the definition itself
    loaders = ("loadFeed", "loadRooms", "loadDeterminations")
    for start in calls[1:]:
        call = _call_text(HTML, start)
        arity = _call_arity(HTML, start)
        assert arity == 8, f"wireWriteAction call passes {arity} args, not 8: {call[:120]!r}"
        assert any(loader in call for loader in loaders), call[:120]


def test_the_unbounded_search_modes_are_disabled_until_a_facet_bounds_them():
    """P-2 (1). `mode=vector` / `mode=summary` with no filters scan the
    whole corpus through an `IN (...)` list and hit SQLite's
    32,766-variable ceiling (retrieve/engine.py's own B.4b note). Both sat
    one click away in the RANK BY dropdown, and the failure was silent."""
    for mode in ("vector", "summary"):
        opt = re.search(r'<option value="' + mode + r'"[^>]*>', HTML)
        assert opt is not None, f"no {mode} option"
        assert "disabled" in opt.group(0), f"{mode} ships enabled"
        assert "facet" in opt.group(0)
    refresh = _fn_body("refreshSearchModeAvailability")
    # the gate is the same value that goes on the wire, so the dropdown
    # cannot disagree with the request it enables.
    assert "searchLicenseTierFilter()" in refresh
    assert 'sel.value = "auto"' in refresh


def test_the_license_tier_facet_goes_on_the_wire():
    """P-2 (4). serve.py has parsed `license_tier` since Stage 2; the page
    filtered client-side instead, which neither bounds the engine nor makes
    the two modes above safe."""
    search = _fn_body("doSearch")
    assert "params.license_tier = tierFilter.join" in search
    filt = _fn_body("searchLicenseTierFilter")
    # "nothing excluded" must send NO filter -- sending every tier seen so
    # far would silently hide one the next page would have introduced.
    assert "if (!excluded.length) return []" in filt


def test_a_toggled_facet_re_asks_rather_than_re_filters():
    render = _fn_body("renderSearchResults")
    assert "doSearch(lastSearchQuery)" in render
    # and the facet list survives the server-side narrowing
    assert "state.searchTiersSeen" in render


def test_the_search_error_status_is_rendered_where_the_results_would_be():
    """P-2 (2)/(3): the server now ANSWERS an unanswerable query, so the
    page must say so rather than leave the last question's results
    standing under the new one."""
    render = _fn_body("renderSearchResults")
    assert 'resp.status === "search_error"' in render
    assert "0 RESULTS" in render


def test_gates_panel_delivers_decoded_edits_and_an_unverified_count(program_root, platform_root):
    """Sweep §3.10 item 3 (pulled into W3 by spec §5.1): the payload
    contract behind the Console's GATES card and the rail badge. `edits`
    was the last panel field every renderer had to JSON.parse itself."""
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    artifact = create_artifact(
        store, type_key=ids["template"], title="decoded edits", path="artifacts/decoded.md",
        sha256="c" * 64, by_launch=ids["launch"], purpose="C3 payload contract",
    )
    gate = open_gate(store, artifact_id=artifact["artifact_id"])
    submit_gate(store, gate_id=gate["gate_id"], by_launch=ids["launch"])
    record_verdict(
        store, gate_id=gate["gate_id"], verdict="PASS_WITH_EDITS", critic_launch=ids["launch"],
        edits=[{"text": "one", "blocking": True}, {"text": "two", "blocking": False}],
    )
    store.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = data.build_gates_panel(rostore)
    finally:
        rostore.close()
    row = next(r for r in panel["pending_edits"] if r["gate_id"] == gate["gate_id"])
    assert isinstance(row["edits"], list)
    assert [e["text"] for e in row["edits"]] == ["one", "two"]
    assert row["unverified_count"] == 2


def test_decode_json_text_leaves_a_plain_string_alone():
    """The fallback the decode helper is only safe because of: a column
    holding a note must survive untouched, never become None."""
    assert data._decode_json_text("just a note") == "just a note"
    assert data._decode_json_text("") == ""
    assert data._decode_json_text(None) is None
    assert data._decode_json_text('[{"a": 1}]') == [{"a": 1}]


# ===========================================================================
# C7 -- the four write actions in the page, and the Determinations detail.
# Same two halves this file already uses: source invariants over the shipped
# markup, and payload contracts built for real out of trialerror.dashboard.data.
# ===========================================================================


def test_the_four_lane_c_actions_are_posted_from_the_page():
    """A write action that exists server-side and is never called from the
    page is the exact shape the walkthrough complained about ("Decide page:
    empty") -- eight actions live, four drawn disabled."""
    for action in ("prereg-reveal", "memory-resolve", "gate-send-back", "thread-create"):
        assert f'"{action}"' in HTML, f"{action} is never posted from the page"


def test_no_reject_verb_is_drawn():
    """Ruling L-C6. Two buttons used to carry the word: the gate arm's
    permanently-disabled "SEND BACK / REJECT", and the generic no-action arm's
    copy of it. C7 replaced the first with a real SEND BACK and the second
    with a label that names what is true, and added no reject anywhere.

    Asserted on RENDERED text (``text: "..."``), not on the raw file, because
    the comments explaining the removal legitimately quote the old label."""
    drawn = set(re.findall(r'text:\s*"([^"]*)"', HTML))
    assert not [t for t in drawn if "REJECT" in t and "MERGE" not in t], \
        f"a reject verb is drawn: {[t for t in drawn if 'REJECT' in t]}"
    assert "SEND BACK" in drawn
    assert "NO ACTION WIRED HERE" in drawn
    assert '"gate-reject"' not in HTML


def test_the_prereg_reveal_button_is_a_two_click_confirm():
    """L-C3: "destructive verbs always ask twice". The arming lives inside
    `wireWriteAction`, in the ONE listener that submits -- a second listener
    would race it, and which saw the click first would depend on wiring
    order."""
    assert "requireTwoClicks" in HTML
    assert "CONFIRM REVEAL — IRREVERSIBLE" in HTML
    assert 'data-confirm-label' in HTML
    # the arming is inside wireWriteAction, not in a separate handler
    wire = HTML.split("function wireWriteAction(")[1].split("\n  /* ====")[0]
    assert 'getAttribute("data-confirm-label")' in wire
    assert 'setAttribute("data-armed", "true")' in wire
    assert "CONFIRM_ARM_MS" in wire, "an armed button must lapse, not wait forever"
    # ...and the arming is not a SECOND listener. This line used to read
    # `assert "requireTwoClicks" not in wire.split(...)[0] or True`, whose
    # trailing `or True` made it unfailable -- and which was false anyway
    # (the name appears in wireWriteAction's own explanatory comment). The
    # invariant it was reaching for is the one the docstring states: exactly
    # one click handler here, and none in `requireTwoClicks`, which only
    # stamps the label the armed state shows.
    assert len(re.findall(r"addEventListener\(", wire)) == 1
    assert "addEventListener(" not in _fn_body("requireTwoClicks")


def test_the_confirm_validates_before_it_arms():
    """Asking somebody to confirm a submission that was never going to be
    sent is how a confirm dialog trains people to click through it."""
    wire = HTML.split("function wireWriteAction(")[1].split("\n  /* ====")[0]
    arm_block = wire.split('data-confirm-label')[1].split('setAttribute("data-armed"')[0]
    assert "is required." in arm_block, "required fields are checked before arming"


def test_dest_dir_is_never_sent_from_the_page():
    """A caller naming a write path is a path-traversal primitive. The
    server ignores the field; the page must not send it either, or the next
    reader will assume it is honoured."""
    reveal_call = HTML.split('"prereg-reveal"')[1].split("wireWriteAction")[0]
    assert "dest_dir" not in reveal_call


def test_the_prereg_detail_shows_hashes_and_says_the_content_is_sealed():
    """REDESIGN 5.4. The generic renderer would have dumped whatever fields
    the item carries; making the omission EXPLICIT is what keeps it correct
    when somebody adds a field to the builder."""
    fields = HTML.split("function renderDeterminationFields(")[1].split("\n  //:")[0]
    assert "procedure_sha256" in fields and "params_sha256" in fields
    assert "HASHES ONLY" in fields
    assert "escrow_present" in fields


def test_the_memory_conflict_detail_draws_both_bodies():
    """"2 versions of X disagree" is not something anyone can choose
    between."""
    fields = HTML.split("function renderDeterminationFields(")[1].split("\n  //:")[0]
    assert "memory-side-" in fields
    assert "item.versions" in fields
    assert "memory-body" in fields
    keeps = HTML.split('{ keep: "left", label: "KEEP LEFT" }')[1].split("].map(")[0]
    for keep, label in (("right", "KEEP RIGHT"), ("both", "KEEP BOTH")):
        assert f'keep: "{keep}"' in keeps and label in keeps


def test_a_sent_back_edit_shows_its_objection_in_the_detail():
    """The item STAYS in the queue -- a sent-back edit is an unverified one
    -- so the next operator has to see that somebody already said no."""
    assert "▲ SENT BACK · " in HTML
    assert "sent_back_note" in HTML
    assert "sent_back_by_launch" in HTML


def test_the_kinds_that_became_writable_lost_their_disabled_reasons():
    """`DETERM_DISABLED_REASONS` is the "why is this greyed out" table. An
    entry left behind for a kind that now has buttons is a lie the page tells
    itself -- and, since the disabled arm is the `else` branch, dead code
    nobody would notice."""
    table = HTML.split("var DETERM_DISABLED_REASONS = {")[1].split("};")[0]
    assert "prereg_reveal" not in table
    assert "memory_conflict:" not in table
    # the three that genuinely stay disabled keep theirs
    assert "room_escalation" in table
    assert "memory_conflict_candidate" in table
    assert "memory_stale" in table


def _el_call_text(source: str, start: int) -> str:
    """The full ``el(...)`` call beginning at ``start``, parens matched."""
    open_paren = source.index("(", start)
    depth, i = 0, open_paren
    while i < len(source):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                return source[open_paren : i + 1]
        i += 1
    raise AssertionError("unbalanced parens in an el() call")


def _assert_every_button_is_guarded(body: str) -> tuple[dict[str, str], list[str]]:
    """The check itself, taking the function body as an argument so it can be
    run against a MUTATED body -- a test that polices a source invariant is
    worth only as much as the proof that it fails when the invariant does.
    Returns (guarded buttons -> the expression that disables them, literally
    disabled calls)."""
    assert "var writable = writesEnabled();" in body, \
        "the arm's enabled state must come from writesEnabled(), nothing else"

    # names whose value is derived from `writable` -- `canDeliver` is one
    # (`writable && item.request_state === "requested"`), and a future arm
    # may add more without this test having to learn about it.
    writable_derived = {"writable"}
    for m in re.finditer(r"var\s+(\w+)\s*=\s*([^;\n]*)", body):
        if re.search(r"\bwritable\b", m.group(2)):
            writable_derived.add(m.group(1))

    literal, guarded = [], {}
    for m in re.finditer(r'\bel\(\s*"button"', body):
        call = _el_call_text(body, m.start())
        line_start = body.rfind("\n", 0, m.start()) + 1
        prefix = body[line_start : m.start()]
        if re.search(r"\bdisabled\s*:", call):
            assert re.search(r"\btitle\s*:", call), (
                "a permanently disabled button must say why it is disabled "
                f"(12.11); this one does not: {call[:120]!r}"
            )
            literal.append(call)
            continue
        name_m = re.search(r"(?:var\s+)?(\w+)\s*=\s*$", prefix)
        assert name_m is not None, (
            "a button this test cannot follow -- it is neither literally "
            "disabled nor assigned to a name whose .disabled it could check: "
            f"{prefix.strip()!r} {call[:100]!r}"
        )
        name = name_m.group(1)
        assign = re.search(rf"\b{re.escape(name)}\.disabled\s*=\s*([^;\n]+)", body)
        assert assign is not None, f"button {name!r} never has its .disabled set"
        assert any(re.search(rf"\b{re.escape(w)}\b", assign.group(1)) for w in sorted(writable_derived)), (
            f"button {name!r} is disabled by {assign.group(1).strip()!r}, which is not "
            f"derived from writesEnabled() (known: {sorted(writable_derived)})"
        )
        guarded[name] = assign.group(1).strip()
    return guarded, literal


def test_every_determination_button_takes_its_disabled_state_from_writesEnabled():
    """The half of spec section 4's "every new button carries `disabled` in a
    snapshot" that a SNAPSHOT CANNOT SEE.

    ``tests/test_dashboard_export.py`` reads the exported markup, so it pins
    the controls that exist as raw HTML (TRANSMIT, + NEW THREAD) and, through
    the ``writesEnabled: writesEnabled()`` wire, the ones a renderer draws
    from a flag. The Determinations arms are neither: ``buildDeterminationActions``
    builds prereg-reveal, gate-verify, gate-send-back and the three
    memory-keep buttons with ``el()`` at render time, inside a detail pane a
    static export never opens. Nothing pinned that they take
    ``!writesEnabled()``, so a future arm could ship an ENABLED destructive
    button into a read-only page with the whole suite green.

    So: read the function's own source and require, for every button it
    draws, either a literal ``disabled`` (a control with no callable, which
    must also carry the ``title`` saying why -- convention 12.11) or a
    ``.disabled =`` assignment whose right-hand side is derived from
    ``writable``."""
    body = _fn_body("buildDeterminationActions")
    guarded, literal = _assert_every_button_is_guarded(body)

    # A test that finds nothing must not pass. Six wired controls plus the
    # two permanently-disabled placeholders is what C6/C7 shipped; MORE is
    # fine, fewer means the loop above stopped seeing the buttons it is
    # supposed to police.
    assert len(guarded) >= 6, f"only found {sorted(guarded)}"
    assert len(literal) >= 2
    for role in ("prereg-reveal-btn", "gate-verify-btn", "gate-send-back-btn",
                 "memory-keep-", "MARK DELIVERED", "ACCEPT MERGE"):
        assert role in body, f"{role} is no longer drawn by this function"


@pytest.mark.parametrize(
    ("what", "find", "replace"),
    [
        ("a new arm forgets the guard", "      verifyBtn.disabled = !writable;\n", ""),
        (
            "a button is disabled by something other than writesEnabled()",
            "verifyBtn.disabled = !writable;",
            "verifyBtn.disabled = item.locked;",
        ),
        (
            "a permanently disabled button stops saying why",
            'disabled: "disabled",\n          title: reason, text: "NO ACTION WIRED HERE",',
            'disabled: "disabled",\n          text: "NO ACTION WIRED HERE",',
        ),
    ],
)
def test_the_disabled_button_check_actually_fails_when_the_guard_goes(what, find, replace):
    """The test above polices a source invariant, which is worth nothing
    unless it BREAKS when the invariant does. Three ways a future edit could
    ship an enabled destructive button into a read-only page, each applied to
    the real function body and each required to raise."""
    body = _fn_body("buildDeterminationActions")
    mutated = body.replace(find, replace)
    assert mutated != body, f"the mutation for {what!r} no longer applies -- rewrite it"
    with pytest.raises(AssertionError):
        _assert_every_button_is_guarded(mutated)


def test_the_gate_arms_two_verbs_are_bound_to_v_and_b():
    """Spec section 4: "buttons VERIFY EDIT / SEND BACK (keys V / B)". C7
    shipped the buttons and not the keys (lane C, finding F6); the Decide
    keydown handler only did j/k row navigation."""
    table = HTML.split("var DETERM_KEY_ROLES = {")[1].split("};")[0]
    assert 'v: "gate-verify-btn"' in table
    assert 'b: "gate-send-back-btn"' in table
    assert table.count(":") == 2, f"only the gate arm's two verbs get a key: {table!r}"

    handler = _fn_body("pressDeterminationVerb")
    assert '[data-role="determ-detail"]' in handler, \
        "the lookup must be scoped to the detail pane -- that is what makes the key act on the SELECTED row"
    assert "btn.disabled" in handler
    assert 'hasAttribute("data-confirm-label")' in handler
    assert "btn.click()" in handler


def test_no_keyboard_path_reaches_a_two_click_verb():
    """The guard that matters, and the reason it is a `data-confirm-label`
    check rather than a list of safe roles. `requireTwoClicks` stamps that
    attribute on every irreversible verb (today only REVEAL), so a future arm
    that arms a destructive button gets no keyboard path to it by
    construction -- and L-C3's "two clicks, from the one listener that
    submits" keeps meaning what it says.

    The page-wide count is the other half: ONE synthetic click exists, and it
    is the one inside `pressDeterminationVerb`. A second would be a second
    submit path, which is exactly what the reveal must not have."""
    assert HTML.count(".click()") == 1
    assert ".click()" in _fn_body("pressDeterminationVerb")
    assert "dispatchEvent" not in HTML

    decide = HTML.split("DETERM_KEY_ROLES[")[1].split("});")[0]
    # the shortcut branch sits AFTER the focus guard, so typing a note that
    # contains "b" cannot send the edit back
    guard_pos = HTML.index('if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;',
                           HTML.index("var DETERM_KEY_ROLES"))
    role_pos = HTML.index("DETERM_KEY_ROLES[(e.key")
    assert guard_pos < role_pos, "a key pressed inside an input must never fire a write"
    assert "e.altKey || e.ctrlKey || e.metaKey" in HTML[guard_pos:role_pos], \
        "browser and OS shortcuts must not be hijacked"
    assert "preventDefault" in decide


def test_the_two_verbs_say_which_key_presses_them():
    """A shortcut nobody can discover is not a shortcut."""
    arm = _fn_body("buildDeterminationActions")
    assert 'verifyBtn.title = "V"' in arm
    assert 'sendBackBtn.title = "B"' in arm


def test_new_thread_is_disabled_in_the_raw_markup_and_synced_by_the_renderer():
    """`state.mode` is not known when the wiring runs, so the honest default
    in the markup is disabled, and `renderFeed` -- where TRANSMIT's own
    enablement is decided -- is what turns it on."""
    for role in ("feed-new-thread-toggle", "feed-new-thread-btn"):
        m = re.search(rf'<[^>]*data-role="{role}"[^>]*>', HTML)
        assert m is not None, role
        assert "disabled" in m.group(0), role
    assert "syncNewThreadEnabled" in HTML
    render_feed = HTML.split("function renderFeed(")[1].split("\n  function ")[0]
    assert "syncNewThreadEnabled()" in render_feed


# ---------------------------------------------------------------------------
# payload contracts -- the half that actually rots
# ---------------------------------------------------------------------------


def test_the_determination_fields_the_page_reads_are_the_fields_the_builder_emits(
    program_root, platform_root
):
    """Built for real out of a seeded store: a gate edit with an objection on
    it, a committed pre-registration, and the keys each detail arm reads."""
    from trialerror.artifacts.gates import open_gate, record_verdict, send_back_edit, submit_gate
    from trialerror.artifacts.registry import create_artifact
    from trialerror.dashboard.store_ro import open_store_ro
    from trialerror.stores.store import open_store
    from trialerror.verify.prereg import commit_prereg
    from tests._store_fixtures import populate_one_of_everything

    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    artifact = create_artifact(
        store, type_key=ids["template"], title="c7 artifact", path="artifacts/c7.md",
        sha256="b" * 64, by_launch=ids["launch"], purpose="c7 contract test",
    )
    gate = open_gate(store, artifact_id=artifact["artifact_id"])
    submit_gate(store, gate_id=gate["gate_id"], by_launch=ids["launch"])
    verdict = record_verdict(
        store, gate_id=gate["gate_id"], verdict="PASS_WITH_EDITS", critic_launch=ids["launch"],
        edits=[{"text": "fix it", "blocking": True}],
    )
    edit_id = json.loads(verdict["edits"])[0]["edit_id"]
    send_back_edit(
        store, gate_id=gate["gate_id"], edit_id=edit_id, by_launch=ids["launch"], note="not yet",
    )
    commit_prereg(store, title="sealed", procedure="do the thing", params={})
    store.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = data.build_determinations_panel(rostore)
    finally:
        rostore.close()

    gate_item = next(i for i in panel["items"] if i["kind"] == "gate_edit" and i["edit_id"] == edit_id)
    assert gate_item["sent_back"] is True
    assert gate_item["sent_back_note"] == "not yet"
    assert gate_item["sent_back_by_launch"] == ids["launch"]
    assert gate_item["sent_back_ts"]

    prereg_item = next(i for i in panel["items"] if i["kind"] == "prereg_reveal" and i["title"] == "sealed")
    assert prereg_item["procedure_sha256"] and prereg_item["params_sha256"]
    assert prereg_item["escrow_present"] is True
    assert "procedure" not in prereg_item, "the sealed content never reaches the wire"


def test_a_missing_escrow_is_reported_on_the_item_not_discovered_on_the_click(
    program_root, platform_root
):
    """`escrow_present` is a stat, and the consequence line says what a reveal
    would DO -- void the commitment -- rather than leaving the operator to
    find out by pressing the button."""
    from trialerror.dashboard.store_ro import open_store_ro
    from trialerror.stores.store import open_store
    from trialerror.verify.prereg import commit_prereg
    from tests._store_fixtures import populate_one_of_everything

    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    row = commit_prereg(store, title="doomed", procedure="p", params={})
    store.close()
    Path(row["escrow_path"]).unlink()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = data.build_determinations_panel(rostore)
    finally:
        rostore.close()
    item = next(i for i in panel["items"] if i["kind"] == "prereg_reveal" and i["title"] == "doomed")
    assert item["escrow_present"] is False
    assert "VOID" in item["consequence"]


def test_memory_conflict_items_carry_both_sides_with_their_bodies(program_root, platform_root, tmp_path):
    from trialerror.dashboard.store_ro import open_store_ro
    from trialerror.memory.api import put_item
    from trialerror.memory.render import export_memory, import_memory
    from trialerror.stores.store import open_store
    from tests._memory_fixtures import make_account
    from tests._store_fixtures import populate_one_of_everything

    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()

    other_root = tmp_path / "other"
    other_root.mkdir()
    other = open_store(other_root, platform_root=platform_root)
    mine = open_store(program_root, platform_root=platform_root)
    try:
        put_item(other, key="k", tier="L0", kind="rule", body="THEIR body",
                 account_id=make_account(other, label="other"))
        put_item(mine, key="k", tier="L0", kind="rule", body="MY body",
                 account_id=make_account(mine, label="mine"))
        export_dir = tmp_path / "e"
        export_memory(other, out_dir=export_dir)
        import_memory(mine, in_dir=export_dir)
    finally:
        other.close()
        mine.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = data.build_determinations_panel(rostore)
    finally:
        rostore.close()
    item = next(i for i in panel["items"] if i["kind"] == "memory_conflict")
    assert [v["side"] for v in item["versions"]] == ["left", "right"]
    assert {v["body"] for v in item["versions"]} == {"MY body", "THEIR body"}
    for v in item["versions"]:
        for key in ("memory_item_id", "tier", "kind", "account_id", "updated_ts", "l0_abstract"):
            assert key in v, key
    assert item["version_count"] == 2
