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
}
