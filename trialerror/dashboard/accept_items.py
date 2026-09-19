"""This lane's own ``GPU_LIVE_CC_ITEMS``-shaped enumeration
(``trialerror.accept.journeys.GPU_LIVE_CC_ITEMS`` -- read as this build's
reference pattern) for the one dashboard acceptance item that is genuinely
orchestrator/integration territory: a REAL browser exercising the served
page's DOM (tabs actually switch, SSE status chip actually updates on a
live store write, panel tables actually render real rows).

NOT added to ``trialerror.accept.journeys.GPU_LIVE_CC_ITEMS`` itself: this
agent's pathspec-limited write scope is ``trialerror/dashboard/``,
``trialerror/cli/dashboard.py``, static assets under ``trialerror/dashboard/
static/``, and its own tests -- ``trialerror/accept/journeys.py`` belongs to a
different lane. Per this build's own instructions ("add a dashboard entry
to that dict if it's importable, else your own module-level equivalent"),
this module is that "own module-level equivalent": same shape (a plain
``dict[str, str]`` of item-key -> the exact orchestrator step that
discharges it), same consumption pattern (a skip-marked pytest per item,
plus a structural test asserting the 1:1 correspondence) -- see
``tests/test_dashboard_accept_items.py``.

A future session that DOES own ``trialerror/accept/journeys.py`` may fold this
dict's one entry into ``GPU_LIVE_CC_ITEMS`` verbatim; until then this is
the single source of truth for the dashboard's own live-DOM gap.
"""

from __future__ import annotations

__all__ = ["DASHBOARD_LIVE_ITEMS"]

DASHBOARD_LIVE_ITEMS: dict[str, str] = {
    "live_dom_dashboard_serve_real_browser": (
        "LIVE real-browser DOM check of `trialerror dashboard serve` (design Section 11 v1 LIVE "
        "DASHBOARD): start `trialerror dashboard serve --program-root <a real or fixture-populated "
        "program>` and, in an actual browser (not a headless assertion against the JSON panel "
        "endpoints), confirm: (1) every tab (session/budget/jobs/gates/corpus/doctor) actually "
        "switches and renders its panel's data as a real table/list, not just that the "
        "underlying `/dashboard/api/<panel>` endpoint returns 200 with the right JSON shape; "
        "(2) the live-status chip shows 'live', then flips to 'reconnecting' if the server is "
        "killed, matching the origin project's earlier dashboard's own "
        "live/rebuilding/reconnecting/static-fallback chip states (that dashboard's "
        "client-side status script, read as this build's reference); (3) writing "
        "to the watched program's stores (e.g. `trialerror jobs list` after `trialerror jobs "
        "start-worker`, or any CLI write) while the page is open causes the SSE `changed` event "
        "to arrive and the affected panel(s) to re-render with the new data, without a manual "
        "page reload; (4) `trialerror dashboard export` produces a snapshot .html that opens correctly "
        "over `file://` with the embedded static-data badge showing (no fetch/SSE attempted, no "
        "console errors from a doomed network call). Offline proxy already covered: "
        "tests/test_dashboard_serve.py (real subprocess: GET /, GET each panel JSON endpoint, "
        "SSE handshake receiving a `hello` event, clean shutdown) + "
        "tests/test_dashboard_export.py (snapshot file is well-formed HTML containing the "
        "embedded panel JSON) + tests/test_dashboard_data.py (every panel builder exercised "
        "against a fixture store with one of everything, per-field assertions). None of those "
        "can exercise an actual DOM/JS execution environment or a real SSE-driven re-render, "
        "which is exactly what this item names."
    ),
    "live_dom_client_resilience": (
        "LIVE real-browser check of the client-resilience batch (2026-09 dashboard bug sweep, "
        "batch C2: LU-2, LU-3, LU-5, LU-8, LU-12, P-1, M-P-1, M-AC-1, M-LU-7, VA-3, AC-1, AC-2). "
        "tests/test_dashboard_client_resilience.py covers the source invariants and the payload "
        "contracts behind them; a rendered page is what it cannot cover. Serve a real program "
        "(`trialerror dashboard serve --program-root <program>`) and confirm, with the Network "
        "tab open: (1) idle 60 s on Console -> zero /dashboard/api/all refetches, the chip stays "
        "`live` (green) and 'AS OF' holds still; one `trialerror feed post` from a shell -> "
        "exactly ONE refetch and 'AS OF' advances. (2) Kill the server: the chip turns amber and "
        "reads `reconnecting` within a few seconds, and after six consecutive stream errors "
        "reads 'reconnecting - retrying'; restart it -> the chip returns to `live` and the page "
        "refetches WITHOUT a reload, 'AS OF' advancing (LU-2/LU-3, the core of this item). "
        "(3) With the server still down, click RETRY on the fetch-error banner -> one immediate "
        "attempt; the banner clears by itself once the server is back. (4) The AC-1 repro: "
        "`trialerror room create` with two discussion points, score neither, load #rooms -> the "
        "discussion-point ladder renders both rows as NOT SCORED and the SCORE THE ROUND / "
        "FREEZE / EXPORT block BELOW the ladder is present (before the fix a null agreement_pct "
        "threw and that block never rendered); Home's live-room card reads 'NOT SCORED' rather "
        "than losing the card. (5) Deep-link an ext panel (#ext-<name>) and reload -> that panel "
        "opens, not Home; kill and restart the server while on it -> it is still selected after "
        "the reconnect (M-P-1). (6) `trialerror dashboard export`, open the snapshot over "
        "file://, click a room other than the baked one -> the 'this snapshot holds only <id>' "
        "notice, never another room's transcript under the clicked room's highlight (M-AC-1). "
        "A DOM-level harness for the pure halves of this (computeHealth's matrix, sweep test 22) "
        "arrives with tests/_dom_shim.js in step C4."
    ),
    "live_dom_console_renderer": (
        "LIVE real-browser check of the Console renderer (2026-09 dashboard bug sweep, batch K4: "
        "console-1 through console-11, M-CON-3/4/5, VA-8, LU-11, M-P-2, M-VA-1). "
        "tests/test_dashboard_console_render.py runs all eight cards under Node against a DOM "
        "shim, which covers the tree each card produces and nothing about how it LOOKS or "
        "whether it updates. Serve a real program (`trialerror dashboard serve --program-root "
        "<program>`), open Console, and confirm: (1) the SESSION card's close-readiness callout "
        "is the FIRST thing in the card body, with the backend's own problem sentences verbatim "
        "and one row per dangling launch below it; the readings grid needs no scrolling to "
        "reach. (2) The idle-gap timeline draws real lanes with real clock times on the axis, "
        "the collapsed markers read `Nh Nm idle`, and the ages tick once a second while the "
        "panel is open and stop when you switch away (watch the SESSION card's OPEN reading "
        "count up). (3) Enqueue a job from a shell (`trialerror jobs enqueue ...`) -> its row "
        "appears with an up arrow in the delta column and the row flares; let it complete -> the "
        "same row shows a down arrow on the next refresh, and NEITHER marker appears on a fresh "
        "page load. (4) Book a launch that is not spawned (`trialerror budget book ...`) -> the "
        "rail health badge flips to WARN and the Console subbar tally follows, both WITHOUT a "
        "reload. (5) Click RUN THE CHECK SWEEP in the DIAGNOSTICS card head -> the card repaints "
        "with FAIL rows first, then WARN, then SKIP, with the PASS rows inside a collapsed "
        "disclosure, and the health badge recomputes from the new sweep. (6) Narrow the window "
        "below 1120px -> the three-column grid becomes one column and the jobs table scrolls "
        "inside its own container rather than shrinking its headers to one character wide "
        "(console-3 / M-CON-4, the finder's own repro). (7) `trialerror dashboard export` -> the "
        "snapshot renders every card over file://, the timeline included, with RUN THE CHECK "
        "SWEEP drawn disabled and saying why."
    ),
    "live_dom_evidence_trace": (
        "LIVE real-browser check of the EVIDENCE surface (lane C step C6, spec section 1; "
        "orchestrator browser checklist item 4). tests/test_dashboard_evidence.py covers the "
        "builder's payload field by field and tests/test_dashboard_evidence_render.py drives the "
        "shipped renderer through the Node DOM shim -- what neither can cover is the rendered "
        "page over a REAL corpus, which is where the readings this surface exists for either "
        "hold or do not. Serve a real program (`trialerror dashboard serve --program-root "
        "<program>`) and confirm: (1) #evidence opens on the newest live claim, with the rail "
        "listing claims and their sources -- nothing renders as `{...}` or `[object Object]`. "
        "(2) The selected claim shows WHAT IT STANDS ON with one row per anchor, each carrying "
        "its coordinates and a hash chip; at least one reads `DOC SHA MATCHES`, and an anchor on "
        "a re-ingested document reads `STALE, DOCUMENT RE-INGESTED`. (3) Run a search on the ASK "
        "tab and click TRACE on a result row -> the Evidence tab opens on the claim anchored "
        "there, or says in place that no claim resolves from that anchor. (4) A claim on a "
        "`commercial_restricted` source shows at most 20 words of quote and an `FENCED` chip. "
        "(5) NEIGHBOURHOOD draws an inline diagram when the claim's evidence carries relations, "
        "and THE SAME EDGES AS A TABLE lists the same edges with a working column sort; a claim "
        "with no anchored relations says there is nothing to draw rather than showing an empty "
        "frame. (6) SEND TO DETERMINATIONS and OPEN A ROOM ON IT are visibly disabled and their "
        "tooltips say why. (7) `trialerror dashboard export`, open the snapshot over file:// -> "
        "the Evidence tab renders the baked claim from the bundle and the rail rows are disabled "
        "with their reason, no fetch attempted."
    ),
    "live_dom_decide_write_actions": (
        "LIVE real-browser check of the four write actions lane C step C7 wired (spec section 4; "
        "orchestrator browser checklist item 6). RUN THIS ON A SCRATCH COPY OF A PROGRAM, never "
        "the real one: a pre-registration reveal cannot be undone, and a memory-conflict "
        "resolution is one-shot per group. tests/test_dashboard_writes.py covers every action's "
        "success/refusal/missing-field path in process, and "
        "tests/test_dashboard_serve.py::test_dashboard_write_actions_full_loop_subprocess drives "
        "all six round trips over real HTTP -- what neither can cover is the two-click confirm, "
        "which is a browser interaction and nothing else. Serve the scratch copy and confirm: "
        "(1) Decide shows a pre-registration as HASHES ONLY, with no procedure text anywhere on "
        "the page; press REVEAL -> the button becomes CONFIRM REVEAL - IRREVERSIBLE and turns "
        "red; wait six seconds without clicking -> it goes back to REVEAL (the arm lapses); press "
        "REVEAL then CONFIRM -> the procedure and params appear, the file path is shown, and the "
        "item leaves the queue. (2) A memory conflict shows TWO bodies side by side; press KEEP "
        "LEFT -> the group leaves the queue; press it again on a second conflict and then try to "
        "resolve the same group twice -> the second attempt is refused in the message strip, not "
        "silently applied. (3) A gate edit: press SEND BACK with the note field empty -> the page "
        "refuses and focuses the note, no request sent; type a note and press SEND BACK -> the "
        "item STAYS in the queue and now reads with a SENT BACK banner and the note; then press "
        "VERIFY EDIT on it -> that succeeds and the item leaves. (4) Type a launch id that does "
        "not exist and press SEND BACK -> a named refusal naming the id, and nothing recorded. "
        "(5) No REJECT button exists anywhere on Decide. (6) The Feed rail's + NEW THREAD opens a "
        "title/body form; submit it -> the new thread appears in the rail, is selected, shows the "
        "first post, and is authored orchestrator:<your open session>. (7) `trialerror dashboard "
        "export`, open the snapshot over file:// -> + NEW THREAD is disabled with its reason, and "
        "every Decide button is disabled. (8) The V / B shortcuts (C9, finding F6): with a gate "
        "edit selected and focus OUTSIDE the note field, press B -> the same send-back the button "
        "does; press V -> the same verify. With the cursor IN the note field, typing 'v' and 'b' "
        "must type letters and send nothing. On a pre-registration item, no key reveals anything: "
        "REVEAL is armed by `data-confirm-label` and the keyboard refuses those on sight."
    ),
    "live_dom_threaded_feed": (
        "LIVE real-browser check of the threaded feed (lane C item B, spec section 2; ruling "
        "L-C4; orchestrator browser checklist item 5). tests/test_dashboard_feed_render.py runs "
        "the shipped static/feed_render.js under the Node DOM shim and pins the tree every "
        "fixture produces; tests/test_dashboard_data_v2.py pins the server's derived shape. "
        "Neither can show that the result is READABLE, which is the whole complaint this item "
        "answers ('messages are not structured by their relationships'). Serve a program whose "
        "feed has real replies (`trialerror dashboard serve --program-root <program>`) and "
        "confirm on the Feed tab: (1) THREADED is the order on first load, with no stored "
        "preference -- replies sit indented under the post they answer, four rails deep at most; "
        "(2) click AS IT ARRIVED, reload the page -> it is STILL as-arrived (localStorage, per "
        "viewer); click THREADED, reload -> still threaded; (3) a root's `N REPLIES` control "
        "collapses its whole subtree and the count matches the number of cards that disappear, "
        "and the 'N REPLIES ARE COLLAPSED' line appears; (4) a reply's `↳ <kind> · HH:MM` head "
        "link scrolls to its parent and flashes it; (5) REPLY IN THREAD on a chosen post shows "
        "the 'replying to ... ✕' strip above the composer, TRANSMIT lands the new post indented "
        "UNDER that parent (not at the bottom at root level), and the strip clears; (6) the "
        "PLAIN ENGLISH column still toggles in both orders, a translated reply three levels deep "
        "renders its two columns STACKED rather than as two twenty-character ones, and a post "
        "the faithfulness gate withheld still says so at every depth; (7) a reply whose parent is "
        "in another thread renders at root level with '↳ replying to a post outside this thread' "
        "-- visible, never dropped; (8) `trialerror dashboard export`, opened over file:// -> the "
        "threaded stream renders out of the bundle and every REPLY IN THREAD is disabled with "
        "its reason in the title."    ),
}
